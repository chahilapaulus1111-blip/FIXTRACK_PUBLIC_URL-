"""Persistent repair alerts and a retryable HTTPS email outbox."""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parseaddr
from urllib.error import HTTPError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen


def timestamp(delay=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat(timespec="microseconds")


def init_db(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS notifications (
            id TEXT PRIMARY KEY,
            recipient_email TEXT NOT NULL,
            job_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            title TEXT NOT NULL,
            message TEXT NOT NULL,
            link TEXT NOT NULL,
            created_at TEXT NOT NULL,
            read_at TEXT,
            email_status TEXT NOT NULL DEFAULT 'pending',
            attempts INTEGER NOT NULL DEFAULT 0,
            retry_at TEXT NOT NULL,
            last_error TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS notification_recipient ON notifications(recipient_email, created_at);
        CREATE INDEX IF NOT EXISTS notification_outbox ON notifications(email_status, retry_at);
    """)


def email_provider():
    return os.environ.get("FIXTRACK_EMAIL_PROVIDER", "resend").strip().lower()


def email_configuration():
    origin = os.environ.get("FIXTRACK_PUBLIC_URL", "").strip().rstrip("/")
    try:
        parsed = urlsplit(origin)
        parsed.port  # Reject malformed ports before constructing any email links.
        valid_origin = (
            (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}))
            and bool(parsed.hostname)
            and not any(character.isspace() for character in origin)
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path)
        )
    except ValueError:
        return False
    key_name = {"resend": "RESEND_API_KEY", "brevo": "BREVO_API_KEY"}.get(email_provider())
    return bool(valid_origin and key_name and os.environ.get(key_name, "").strip() and os.environ.get("FIXTRACK_EMAIL_FROM", "").strip())


def queue_changes(connection, previous, updated):
    """Called inside the workspace transaction, covering bulk and individual APIs."""
    old_jobs = {job["id"]: job for job in previous.get("jobs", [])}
    staff = connection.execute("SELECT name,email,role FROM users WHERE active = 1 AND role IN ('admin','technician')").fetchall()
    for job in updated.get("jobs", []):
        old = old_jobs.get(job["id"])
        if old == job:
            continue
        new_history = job.get("history", [])[len((old or {}).get("history", [])):]
        if old is None:
            kind, title = "created", "New repair registered"
        elif job.get("decision") in {"Approved", "Declined"} and job.get("decision") != old.get("decision"):
            kind, title = "decision", "Quotation " + job["decision"].lower()
        elif job.get("quote") != old.get("quote") or job.get("work") != old.get("work") or any(str(item.get("text", "")).startswith("Quotation issued:") for item in new_history):
            kind, title = "quotation", "Quotation ready for review"
        elif job.get("tech") != old.get("tech"):
            kind, title = "assignment", "Repair technician changed"
        else:
            kind, title = "updated", "Repair updated"
        recipients = {str(job.get("customerEmail", "")).strip().lower(): "customer"}
        recipients.update({person["email"].lower(): person["role"] for person in staff if person["role"] == "admin" or person["name"] == job.get("tech")})
        for email, role in recipients.items():
            if "@" not in email:
                continue
            message = f"{job['id']} - {job.get('device', 'Repair')}. Status: {job.get('status', 'Checked in')}. Technician: {job.get('tech', 'Unassigned')}."
            if kind == "created":
                message += " A repair job is connected to this email address." if role == "customer" else " Review the intake and issue a quotation." if role == "admin" else " This repair has been assigned to you."
            if job.get("quote", 0) > 0:
                message += f" Quotation: N$ {float(job['quote']):.2f} ({job.get('decision', 'Pending')})."
            if kind == "quotation" and role == "customer":
                message += " Sign in to approve or decline this quotation."
            if new_history and kind == "updated":
                message += " " + str(new_history[-1].get("text", ""))[:500]
            now = timestamp()
            connection.execute(
                "INSERT INTO notifications(id,recipient_email,job_id,kind,title,message,link,created_at,retry_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, email, job["id"], kind, title, message, "/?job=" + quote(job["id"], safe=""), now, now),
            )


def inbox(connection, email):
    rows = connection.execute(
        "SELECT id,job_id,kind,title,message,link,created_at,read_at FROM notifications WHERE recipient_email = ? ORDER BY created_at DESC,id DESC LIMIT 100",
        (email.lower(),),
    ).fetchall()
    unread = connection.execute("SELECT COUNT(*) AS count FROM notifications WHERE recipient_email = ? AND read_at IS NULL", (email.lower(),)).fetchone()["count"]
    return {"notifications": [dict(row) for row in rows], "unread": unread}


def delivery_status(connection):
    counts = {row["email_status"]: row["count"] for row in connection.execute("SELECT email_status,COUNT(*) AS count FROM notifications GROUP BY email_status").fetchall()}
    return {"configured": email_configuration(), "provider": email_provider() if email_provider() in {"resend", "brevo"} else None, "pending": counts.get("pending", 0) + counts.get("sending", 0), "failed": counts.get("failed", 0), "sent": counts.get("sent", 0)}


def send_email(item):
    if not email_configuration():
        raise ValueError("Email delivery is not configured")
    sender = os.environ["FIXTRACK_EMAIL_FROM"].strip()
    subject = f"FixTrack {item['job_id']}: {item['title']}"
    body = item["message"] + "\n\nTrack your repair: " + os.environ["FIXTRACK_PUBLIC_URL"].strip().rstrip("/") + item["link"] + "\n\nSign in using this email address. New customer accounts require workshop verification. Quotation decisions are made inside FixTrack."
    headers = {"Content-Type": "application/json", "User-Agent": "FixTrack/1.0"}
    if email_provider() == "brevo":
        name, address = parseaddr(sender)
        identity = {"name": name or "FixTrack", "email": address}
        payload = {"sender": identity, "replyTo": identity, "to": [{"email": item["recipient_email"]}], "subject": subject, "textContent": body, "headers": {"idempotencyKey": item["id"]}}
        headers["api-key"] = os.environ["BREVO_API_KEY"].strip()
        endpoint, response_key = "https://api.brevo.com/v3/smtp/email", "messageId"
    else:
        payload = {"from": sender, "to": [item["recipient_email"]], "subject": subject, "text": body}
        headers.update({"Authorization": "Bearer " + os.environ["RESEND_API_KEY"].strip(), "Idempotency-Key": "fixtrack/" + item["id"]})
        endpoint, response_key = "https://api.resend.com/emails", "id"
    outgoing = Request(endpoint, data=json.dumps(payload).encode("utf-8"), method="POST", headers=headers)
    with urlopen(outgoing, timeout=15) as response:
        if not json.load(response).get(response_key):
            raise ValueError("Email provider did not accept the message")


def deliver_one(connect_db):
    """Claim with a lease so multiple workers cannot send the same row together."""
    if not email_configuration():
        return False
    now = timestamp()
    with connect_db() as connection:
        item = connection.execute("SELECT * FROM notifications WHERE email_status IN ('pending','failed','sending') AND attempts < 8 AND retry_at <= ? ORDER BY created_at,id LIMIT 1", (now,)).fetchone()
        if item is None:
            return False
        claimed = connection.execute("UPDATE notifications SET email_status = 'sending', attempts = attempts + 1, retry_at = ? WHERE id = ? AND email_status = ? AND attempts = ? AND retry_at <= ?", (timestamp(300), item["id"], item["email_status"], item["attempts"], now))
        if claimed.rowcount != 1:
            return True
        item = dict(item)
    try:
        send_email(item)
    except Exception as error:
        # Do not persist provider response bodies, API keys or customer addresses in errors.
        reason = f"Email provider HTTP {error.code}" if isinstance(error, HTTPError) else type(error).__name__
        with connect_db() as connection:
            connection.execute("UPDATE notifications SET email_status = 'failed', last_error = ?, retry_at = ? WHERE id = ?", (reason, timestamp(min(3600, 30 * 2 ** item["attempts"])), item["id"]))
    else:
        with connect_db() as connection:
            connection.execute("UPDATE notifications SET email_status = 'sent', last_error = '' WHERE id = ?", (item["id"],))
    return True


_worker_lock = threading.Lock()
_worker_running = False


def start_worker(connect_db, logger):
    global _worker_running
    if not email_configuration():
        return
    with _worker_lock:
        if _worker_running:
            return
        _worker_running = True

    def work():
        global _worker_running
        try:
            while email_configuration():
                if deliver_one(connect_db):
                    time.sleep(0.6)
                    continue
                with connect_db() as connection:
                    remaining = connection.execute("SELECT 1 FROM notifications WHERE email_status IN ('pending','failed','sending') AND attempts < 8 LIMIT 1").fetchone()
                if not remaining:
                    break
                time.sleep(30)
        except Exception:
            logger.exception("Email worker stopped; queued alerts will resume on the next signed-in request")
        finally:
            with _worker_lock:
                _worker_running = False

    threading.Thread(target=work, name="fixtrack-email", daemon=True).start()
