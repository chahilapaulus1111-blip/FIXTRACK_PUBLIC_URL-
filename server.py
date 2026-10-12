from __future__ import annotations

import hashlib
import json
import os
import secrets
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from functools import wraps
from getpass import getpass
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory, session
import psycopg
from psycopg.rows import dict_row
from werkzeug.security import check_password_hash, generate_password_hash

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)
DATABASE = INSTANCE_DIR / "fixtrack.sqlite3"
DATABASE_URL = os.environ.get("DATABASE_URL", "").strip()
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql://" + DATABASE_URL[len("postgres://"):]
USE_POSTGRES = DATABASE_URL.startswith("postgresql://")
INTEGRITY_ERRORS = (sqlite3.IntegrityError, psycopg.IntegrityError)
SECRET_FILE = INSTANCE_DIR / "secret.key"
STATIC_FILES = {
    "index.html",
    "app.js",
    "style.css",
    "mobile-fixes.css",
    "auth.css",
    "manifest.webmanifest",
    "service-worker.js",
    "icons/fixtrack.svg",
    "icons/fixtrack-192.png",
    "icons/fixtrack-512.png",
}
STAGES = ["Checked in", "Diagnosing", "Awaiting approval", "In repair", "Ready for collection", "Collected"]

if os.environ.get("SECRET_KEY"):
    SECRET_KEY = os.environ["SECRET_KEY"]
elif SECRET_FILE.exists():
    SECRET_KEY = SECRET_FILE.read_text(encoding="utf-8").strip()
else:
    SECRET_KEY = secrets.token_hex(32)
    SECRET_FILE.write_text(SECRET_KEY, encoding="utf-8")

app = Flask(__name__)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    MAX_CONTENT_LENGTH=1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("FIXTRACK_COOKIE_SECURE") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(days=7),
)


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class PostgresConnection:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        self.connection.__enter__()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return self.connection.__exit__(exc_type, exc_value, traceback)
        finally:
            self.connection.close()

    def execute(self, query, params=()):
        if query.strip().upper() == "BEGIN IMMEDIATE":
            query = "BEGIN"
        return self.connection.execute(query.replace("?", "%s"), params)

    def executescript(self, script):
        for statement in script.split(";"):
            if statement.strip():
                self.execute(statement)

    def close(self):
        self.connection.close()


def connect_db():
    if USE_POSTGRES:
        return PostgresConnection(psycopg.connect(DATABASE_URL, row_factory=dict_row))
    connection = sqlite3.connect(DATABASE, timeout=10, factory=ClosingConnection)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def init_db() -> None:
    if USE_POSTGRES:
        schema = """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('admin', 'technician', 'customer')),
            active INTEGER NOT NULL DEFAULT 1,
            password_reset_required INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sessions (
            token_hash TEXT PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            expires_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS workspace (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            payload TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 0
        );
        """
        with connect_db() as connection:
            connection.executescript(schema)
            connection.execute(
                "INSERT INTO workspace(id,payload) VALUES(1,?) ON CONFLICT(id) DO NOTHING",
                ('{"jobs":[],"parts":[],"activity":[]}',),
            )
            user_columns = {
                row["column_name"] for row in connection.execute(
                    "SELECT column_name FROM information_schema.columns WHERE table_name = 'users'"
                ).fetchall()
            }
            if "active" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
            if "password_reset_required" not in user_columns:
                connection.execute("ALTER TABLE users ADD COLUMN password_reset_required INTEGER NOT NULL DEFAULT 0")
        return

    with connect_db() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin', 'technician', 'customer')),
                active INTEGER NOT NULL DEFAULT 1,
                password_reset_required INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                expires_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS workspace (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                payload TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 0
            );
            INSERT OR IGNORE INTO workspace(id, payload) VALUES (1, '{"jobs":[],"parts":[],"activity":[]}');
            """
        )
        user_columns = {row["name"] for row in connection.execute("PRAGMA table_info(users)")}
        if "active" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")
        if "password_reset_required" not in user_columns:
            connection.execute("ALTER TABLE users ADD COLUMN password_reset_required INTEGER NOT NULL DEFAULT 0")


def now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_workspace(connection: sqlite3.Connection | None = None) -> dict:
    owns_connection = connection is None
    connection = connection or connect_db()
    try:
        row = connection.execute("SELECT payload FROM workspace WHERE id = 1").fetchone()
        return json.loads(row["payload"])
    finally:
        if owns_connection:
            connection.close()


def write_workspace(connection: sqlite3.Connection, payload: dict) -> None:
    connection.execute("UPDATE workspace SET payload = ?, version = version + 1 WHERE id = 1", (json.dumps(payload),))


def lock_workspace(connection) -> None:
    if USE_POSTGRES:
        connection.execute("SELECT id FROM workspace WHERE id = 1 FOR UPDATE")


def add_activity(payload: dict, text: str) -> None:
    payload.setdefault("activity", []).insert(0, {"text": text, "date": now_text()})
    del payload["activity"][100:]


def user_json(user: sqlite3.Row) -> dict:
    return {"id": user["id"], "name": user["name"], "email": user["email"], "role": user["role"], "active": bool(user["active"]), "passwordResetRequired": bool(user["password_reset_required"])}


def login_required(handler):
    @wraps(handler)
    def wrapped(*args, **kwargs):
        token = session.get("auth_token")
        if not token:
            return jsonify(error="Sign in required."), 401
        digest = hashlib.sha256(token.encode()).hexdigest()
        with connect_db() as connection:
            row = connection.execute(
                "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
                "WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1",
                (digest, now_text()),
            ).fetchone()
        if not row:
            session.clear()
            return jsonify(error="Your session has expired. Please sign in again."), 401
        if row["password_reset_required"] and request.endpoint not in {"get_session", "logout", "required_password_change"}:
            return jsonify(error="Change your temporary password before continuing."), 428
        g.user = row
        return handler(*args, **kwargs)
    return wrapped


def roles_required(*allowed_roles):
    def decorate(handler):
        @wraps(handler)
        @login_required
        def wrapped(*args, **kwargs):
            if g.user["role"] not in allowed_roles:
                return jsonify(error="You do not have permission to perform this action."), 403
            return handler(*args, **kwargs)
        return wrapped
    return decorate


def json_body() -> dict:
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


def find_job(payload: dict, job_id: str) -> dict | None:
    return next((job for job in payload.get("jobs", []) if job.get("id") == job_id), None)


def active_technician_names(connection: sqlite3.Connection) -> set[str]:
    rows = connection.execute(
        "SELECT name FROM users WHERE role = 'technician' AND active = 1"
    ).fetchall()
    return {row["name"] for row in rows}


def valid_technician_assignment(connection: sqlite3.Connection, name: str) -> bool:
    return name == "Unassigned" or name in active_technician_names(connection)


def append_job_history(job: dict, text: str) -> None:
    job.setdefault("history", []).append({"text": text, "date": now_text()})


@app.before_request
def reject_cross_origin_writes():
    if request.method not in {"POST", "PATCH", "PUT", "DELETE"}:
        return None
    origin = request.headers.get("Origin")
    if origin and origin.rstrip("/") != request.host_url.rstrip("/"):
        return jsonify(error="Cross-origin request rejected."), 403
    return None


@app.get("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


@app.get("/<path:filename>")
def static_file(filename: str):
    if filename not in STATIC_FILES:
        return jsonify(error="Not found."), 404
    return send_from_directory(BASE_DIR, filename)


@app.get("/api/session")
def get_session():
    token = session.get("auth_token")
    if not token:
        return jsonify(user=None)
    digest = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as connection:
        user = connection.execute(
            "SELECT users.* FROM sessions JOIN users ON users.id = sessions.user_id "
            "WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.active = 1",
            (digest, now_text()),
        ).fetchone()
    if not user:
        session.clear()
        return jsonify(user=None)
    return jsonify(user=user_json(user))


@app.get("/api/setup-status")
def setup_status():
    with connect_db() as connection:
        count = connection.execute("SELECT COUNT(*) AS count FROM users WHERE role = 'admin'").fetchone()["count"]
    return jsonify(needsAdmin=count == 0)


@app.post("/api/setup")
def setup_first_admin():
    with connect_db() as connection:
        if connection.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return jsonify(error="Administrator setup is already complete."), 409
    body = json_body()
    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not name or len(name) > 100 or "@" not in email or len(email) > 254:
        return jsonify(error="Enter a valid name and email address."), 400
    if len(password) < 12:
        return jsonify(error="The administrator password must have at least 12 characters."), 400
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if USE_POSTGRES:
            connection.execute("LOCK TABLE users IN EXCLUSIVE MODE")
        if connection.execute("SELECT 1 FROM users WHERE role = 'admin' LIMIT 1").fetchone():
            return jsonify(error="Administrator setup is already complete."), 409
        try:
            connection.execute(
                "INSERT INTO users(name,email,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)",
                (name, email, generate_password_hash(password), "admin", 1, now_text()),
            )
            user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        except INTEGRITY_ERRORS:
            return jsonify(error="An account already exists for that email."), 409
    start_session(user)
    return jsonify(user=user_json(user)), 201


@app.post("/api/signup")
def signup():
    body = json_body()
    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not name or len(name) > 100 or "@" not in email or len(email) > 254:
        return jsonify(error="Enter a valid name and email address."), 400
    if len(password) < 10:
        return jsonify(error="Use a password with at least 10 characters."), 400
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO users(name,email,password_hash,role,active,created_at) VALUES(?,?,?,?,?,?)",
                (name, email, generate_password_hash(password), "customer", 0, now_text()),
            )
        return jsonify(message="Your account request was submitted. An administrator must verify your email before you can sign in."), 202
    except INTEGRITY_ERRORS:
        return jsonify(error="An account already exists for that email."), 409


def start_session(user: sqlite3.Row) -> None:
    token = secrets.token_urlsafe(32)
    digest = hashlib.sha256(token.encode()).hexdigest()
    expiry = datetime.now(timezone.utc) + app.permanent_session_lifetime
    with connect_db() as connection:
        connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (now_text(),))
        connection.execute(
            "INSERT INTO sessions(token_hash,user_id,expires_at) VALUES(?,?,?)",
            (digest, user["id"], expiry.isoformat(timespec="seconds")),
        )
    session.clear()
    session.permanent = True
    session["auth_token"] = token


@app.post("/api/login")
def login():
    body = json_body()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    with connect_db() as connection:
        user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
    if not user or not check_password_hash(user["password_hash"], password):
        return jsonify(error="Email or password is incorrect."), 401
    if not user["active"]:
        return jsonify(error="Your account is awaiting administrator verification."), 403
    start_session(user)
    return jsonify(user=user_json(user))


@app.post("/api/password/change")
@login_required
def change_password():
    body = json_body()
    current_password = str(body.get("currentPassword", ""))
    new_password = str(body.get("newPassword", ""))
    if len(new_password) < 10:
        return jsonify(error="Use a new password with at least 10 characters."), 400
    if not check_password_hash(g.user["password_hash"], current_password):
        return jsonify(error="Your current password is incorrect."), 401
    if check_password_hash(g.user["password_hash"], new_password):
        return jsonify(error="Choose a password different from your current password."), 400
    token = session.get("auth_token", "")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET password_hash = ?, password_reset_required = 0 WHERE id = ?",
            (generate_password_hash(new_password), g.user["id"]),
        )
        connection.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (g.user["id"], token_hash),
        )
    return jsonify(ok=True)


@app.post("/api/password/required-change")
@login_required
def required_password_change():
    body = json_body()
    new_password = str(body.get("newPassword", ""))
    if len(new_password) < 10:
        return jsonify(error="Use a new password with at least 10 characters."), 400
    if not g.user["password_reset_required"]:
        return jsonify(error="No temporary password change is required."), 409
    if check_password_hash(g.user["password_hash"], new_password):
        return jsonify(error="Choose a password different from your temporary password."), 400
    token = session.get("auth_token", "")
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    with connect_db() as connection:
        connection.execute(
            "UPDATE users SET password_hash = ?, password_reset_required = 0 WHERE id = ?",
            (generate_password_hash(new_password), g.user["id"]),
        )
        connection.execute(
            "DELETE FROM sessions WHERE user_id = ? AND token_hash != ?",
            (g.user["id"], token_hash),
        )
        user = connection.execute("SELECT * FROM users WHERE id = ?", (g.user["id"],)).fetchone()
    return jsonify(user=user_json(user))


@app.post("/api/logout")
@login_required
def logout():
    token = session.pop("auth_token", None)
    if token:
        with connect_db() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))
    session.clear()
    return jsonify(ok=True)


@app.get("/api/workspace")
@login_required
def workspace():
    with connect_db() as connection:
        row = connection.execute("SELECT payload,version FROM workspace WHERE id = 1").fetchone()
        payload = json.loads(row["payload"])
        version = row["version"]
        technicians = [] if g.user["role"] == "customer" else [
            {"name": name} for name in sorted(active_technician_names(connection), key=str.casefold)
        ]
    if g.user["role"] == "customer":
        payload["jobs"] = [job for job in payload.get("jobs", []) if job.get("customerEmail", "").lower() == g.user["email"].lower()]
        payload["parts"] = []
        payload["activity"] = []
    return jsonify(user=user_json(g.user), technicians=technicians, version=version, **payload)


@app.put("/api/workspace")
@roles_required("admin", "technician")
def update_workspace():
    body = json_body()
    incoming = {key: body.get(key) for key in ("jobs", "parts", "activity")}
    if not all(isinstance(incoming[key], list) for key in incoming):
        return jsonify(error="Workspace data is malformed."), 400
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        lock_workspace(connection)
        row = connection.execute("SELECT payload,version FROM workspace WHERE id = 1").fetchone()
        if body.get("version") != row["version"]:
            return jsonify(error="Workspace changed elsewhere. Reload and retry your update."), 409
        current = json.loads(row["payload"])
        available_technicians = active_technician_names(connection)
        current_jobs = {job["id"]: job for job in current["jobs"]}
        for job in incoming["jobs"]:
            if not isinstance(job, dict):
                continue
            previous = current_jobs.get(job.get("id"))
            technician = job.get("tech", "Unassigned")
            if (previous is None or technician != previous.get("tech", "Unassigned")) and technician not in available_technicians | {"Unassigned"}:
                return jsonify(error="Assign repairs only to an active technician account."), 400
        if g.user["role"] == "technician":
            current_jobs = {job["id"]: job for job in current["jobs"]}
            incoming_jobs = {job.get("id"): job for job in incoming["jobs"] if isinstance(job, dict) and job.get("id")}
            if len(incoming_jobs) != len(incoming["jobs"]) or not current_jobs.keys() <= incoming_jobs.keys():
                return jsonify(error="Technicians cannot remove or replace repair records."), 403
            allowed_fields = {"status", "tech", "history", "parts"}
            immutable_fields = {"customer", "customerEmail", "device", "fault", "quote", "decision", "work", "phone", "serial", "date", "due"}
            part_usage: dict[str, int] = {}
            for job_id, job in incoming_jobs.items():
                if job_id not in current_jobs:
                    if job.get("status") != "Checked in" or job.get("quote") != 0 or job.get("decision") != "Not quoted" or job.get("parts"):
                        return jsonify(error="New technician-created jobs must start unquoted with no parts."), 403
                    if any(not str(job.get(field, "")).strip() for field in ("customer", "customerEmail", "device", "fault")):
                        return jsonify(error="New jobs require customer, email, device, and fault details."), 400
                    continue
                previous = current_jobs[job_id]
                if any(job.get(key) != previous.get(key) for key in immutable_fields):
                    return jsonify(error="Technicians cannot edit customer details or quotation data."), 403
                if any(key not in set(previous) | allowed_fields for key in job):
                    return jsonify(error="Technicians cannot add protected repair fields."), 403
                if job.get("status") not in STAGES:
                    return jsonify(error="Invalid repair stage."), 400
                if job["status"] in {"In repair", "Ready for collection", "Collected"} and job.get("decision") != "Approved":
                    return jsonify(error="An approved quotation is required before proceeding."), 409
                old_history, new_history = previous.get("history", []), job.get("history", [])
                if not isinstance(new_history, list) or new_history[:len(old_history)] != old_history:
                    return jsonify(error="Repair history cannot be rewritten."), 403
                old_parts = previous.get("parts", [])
                new_parts = job.get("parts", [])
                if not isinstance(new_parts, list):
                    return jsonify(error="Parts usage is malformed."), 400
                old_counts: dict[str, int] = {}
                new_counts: dict[str, int] = {}
                for item in old_parts:
                    old_counts[item["name"]] = old_counts.get(item["name"], 0) + int(item["qty"])
                for item in new_parts:
                    new_counts[item["name"]] = new_counts.get(item["name"], 0) + int(item["qty"])
                if any(new_counts.get(name, 0) < qty for name, qty in old_counts.items()):
                    return jsonify(error="Existing parts usage cannot be removed."), 403
                for name, qty in new_counts.items():
                    delta = qty - old_counts.get(name, 0)
                    if delta:
                        part_usage[name] = part_usage.get(name, 0) + delta
            old_parts = {str(part["id"]): part for part in current["parts"]}
            new_parts = {str(part.get("id")): part for part in incoming["parts"] if isinstance(part, dict) and part.get("id") is not None}
            if old_parts.keys() != new_parts.keys() or len(new_parts) != len(incoming["parts"]):
                return jsonify(error="Technicians cannot create or remove inventory records."), 403
            for part_id, part in new_parts.items():
                previous = old_parts[part_id]
                if any(part.get(key) != previous.get(key) for key in ("name", "sku", "min", "cost")):
                    return jsonify(error="Technicians cannot change spare-part pricing or details."), 403
                expected_stock = int(previous["stock"]) - part_usage.get(previous["name"], 0)
                if int(part.get("stock", -1)) != expected_stock or expected_stock < 0:
                    return jsonify(error="Stock changes must match parts used on approved repair jobs."), 403
            incoming["activity"] = current["activity"]
            for job_id, job in incoming_jobs.items():
                if job_id in current_jobs:
                    for key in allowed_fields:
                        current_jobs[job_id][key] = job.get(key, current_jobs[job_id].get(key))
                else:
                    current["jobs"].insert(0, job)
            current["parts"] = [{**part, "stock": int(part["stock"])} for part in incoming["parts"]]
            updated = current
        else:
            if len(incoming["jobs"]) > 5000 or len(incoming["parts"]) > 5000:
                return jsonify(error="Workspace exceeds the allowed record limit."), 413
            current_jobs = {job["id"]: job for job in current["jobs"]}
            for job in incoming["jobs"]:
                previous = current_jobs.get(job.get("id"))
                if not previous:
                    if job.get("decision") != "Not quoted" or float(job.get("quote", 0)) != 0:
                        return jsonify(error="New repairs must start without a quotation."), 400
                    continue
                quote_changed = job.get("quote") != previous.get("quote") or job.get("work") != previous.get("work")
                if quote_changed and (job.get("decision") != "Pending" or job.get("status") != "Awaiting approval" or float(job.get("quote", 0)) <= 0):
                    return jsonify(error="A changed quotation must be submitted for customer approval."), 403
                if job.get("decision") != previous.get("decision"):
                    if job.get("decision") != "Pending" or not quote_changed:
                        return jsonify(error="Only the linked customer can approve or decline a quotation."), 403
            updated = incoming
        add_activity(updated, f"Workspace updated by {g.user['name']}")
        write_workspace(connection, updated)
        version = connection.execute("SELECT version FROM workspace WHERE id = 1").fetchone()["version"]
    return jsonify(user=user_json(g.user), version=version, **updated)


@app.post("/api/jobs")
@roles_required("admin", "technician")
def create_job():
    body = json_body()
    required = ["customer", "customerEmail", "device", "fault"]
    if any(not str(body.get(field, "")).strip() for field in required):
        return jsonify(error="Customer, customer email, device, and fault are required."), 400
    if "@" not in body["customerEmail"]:
        return jsonify(error="Enter a valid customer email address."), 400
    with connect_db() as connection:
        technician = str(body.get("tech") or "Unassigned")
        if not valid_technician_assignment(connection, technician):
            return jsonify(error="Assign the repair to an active technician account."), 400
        payload = read_workspace(connection)
        numbers = [int(job["id"].split("-")[1]) for job in payload["jobs"] if job.get("id", "").startswith("FT-") and job["id"].split("-")[1].isdigit()]
        job_id = f"FT-{max(numbers, default=1000) + 1}"
        job = {
            "id": job_id,
            "customer": str(body["customer"]).strip(),
            "customerEmail": str(body["customerEmail"]).strip().lower(),
            "device": str(body["device"]).strip(),
            "fault": str(body["fault"]).strip(),
            "status": "Checked in",
            "tech": technician,
            "quote": 0,
            "decision": "Not quoted",
            "phone": str(body.get("phone", "")).strip(),
            "serial": str(body.get("serial", "")).strip(),
            "date": now_text()[:10],
            "due": str(body.get("due", "")),
            "history": [],
            "parts": [],
        }
        append_job_history(job, "Device checked in")
        payload["jobs"].insert(0, job)
        add_activity(payload, f"{job_id} checked in")
        write_workspace(connection, payload)
    return jsonify(job=job), 201


@app.patch("/api/jobs/<job_id>")
@roles_required("admin", "technician")
def update_job(job_id: str):
    body = json_body()
    with connect_db() as connection:
        payload = read_workspace(connection)
        job = find_job(payload, job_id)
        if not job:
            return jsonify(error="Repair job not found."), 404
        status = body.get("status", job["status"])
        tech = str(body.get("tech", job["tech"]))
        note = str(body.get("note", "")).strip()[:500]
        if tech != job.get("tech", "Unassigned") and not valid_technician_assignment(connection, tech):
            return jsonify(error="Assign the repair to an active technician account."), 400
        if status not in STAGES:
            return jsonify(error="Invalid repair stage."), 400
        if status in {"In repair", "Ready for collection", "Collected"} and job["decision"] != "Approved":
            return jsonify(error="An approved quotation is required before proceeding."), 409
        job["status"] = status
        job["tech"] = tech
        append_job_history(job, status + (f" - {note}" if note else ""))
        add_activity(payload, f"{job_id} updated: {status}")
        write_workspace(connection, payload)
    return jsonify(job=job)


@app.post("/api/jobs/<job_id>/quotation")
@roles_required("admin")
def save_quotation(job_id: str):
    body = json_body()
    try:
        quote = float(body.get("quote", 0))
    except (TypeError, ValueError):
        return jsonify(error="Enter a valid quotation amount."), 400
    work = str(body.get("work", "")).strip()[:500]
    if quote <= 0 or not work:
        return jsonify(error="Quotation amount and work description are required."), 400
    with connect_db() as connection:
        payload = read_workspace(connection)
        job = find_job(payload, job_id)
        if not job:
            return jsonify(error="Repair job not found."), 404
        if job["status"] == "Collected":
            return jsonify(error="Collected jobs cannot be requoted."), 409
        job["quote"] = quote
        job["work"] = work
        job["decision"] = "Pending"
        job["status"] = "Awaiting approval"
        append_job_history(job, f"Quotation issued: N$ {quote:.2f}")
        add_activity(payload, f"Quotation saved for {job_id}")
        write_workspace(connection, payload)
    return jsonify(job=job)


@app.post("/api/jobs/<job_id>/decision")
@roles_required("customer")
def quotation_decision(job_id: str):
    value = json_body().get("decision")
    if value not in {"Approved", "Declined"}:
        return jsonify(error="Decision must be Approved or Declined."), 400
    with connect_db() as connection:
        payload = read_workspace(connection)
        job = find_job(payload, job_id)
        if not job or job.get("customerEmail", "").lower() != g.user["email"].lower():
            return jsonify(error="Repair job not found."), 404
        if job["decision"] != "Pending":
            return jsonify(error="This quotation is no longer awaiting a decision."), 409
        job["decision"] = value
        job["status"] = "In repair" if value == "Approved" else "Diagnosing"
        append_job_history(job, f"Quotation {value.lower()}")
        add_activity(payload, f"{job_id} quotation {value.lower()}")
        write_workspace(connection, payload)
    return jsonify(job=job)


@app.post("/api/jobs/<job_id>/parts")
@roles_required("admin", "technician")
def use_part(job_id: str):
    part_id = json_body().get("part")
    with connect_db() as connection:
        connection.execute("BEGIN IMMEDIATE")
        lock_workspace(connection)
        payload = read_workspace(connection)
        job = find_job(payload, job_id)
        part = next((item for item in payload["parts"] if str(item["id"]) == str(part_id)), None)
        if not job or not part:
            return jsonify(error="Repair job or spare part not found."), 404
        if job["decision"] != "Approved" or job["status"] == "Collected":
            return jsonify(error="Parts require an approved quotation and an open job."), 409
        if int(part["stock"]) < 1:
            return jsonify(error="This part is out of stock."), 409
        part["stock"] = int(part["stock"]) - 1
        job.setdefault("parts", []).append({"name": part["name"], "qty": 1})
        append_job_history(job, f"Used 1 x {part['name']}")
        add_activity(payload, f"Part recorded for {job_id}")
        write_workspace(connection, payload)
    return jsonify(job=job)


@app.post("/api/parts")
@roles_required("admin")
def create_part():
    body = json_body()
    name, sku = str(body.get("name", "")).strip(), str(body.get("sku", "")).strip()
    try:
        stock, minimum, cost = int(body.get("stock", 0)), int(body.get("min", 0)), float(body.get("cost", 0))
    except (TypeError, ValueError):
        return jsonify(error="Enter valid stock, reorder, and cost values."), 400
    if not name or not sku or stock < 0 or minimum < 0 or cost < 0:
        return jsonify(error="Part details and non-negative values are required."), 400
    with connect_db() as connection:
        payload = read_workspace(connection)
        if any(part["sku"].lower() == sku.lower() for part in payload["parts"]):
            return jsonify(error="That SKU already exists."), 409
        part = {"id": secrets.randbelow(2_000_000_000) + 1, "name": name, "sku": sku, "stock": stock, "min": minimum, "cost": cost}
        payload["parts"].append(part)
        add_activity(payload, f"Added {name}")
        write_workspace(connection, payload)
    return jsonify(part=part), 201


@app.post("/api/parts/<int:part_id>/restock")
@roles_required("admin")
def restock_part(part_id: int):
    try:
        quantity = int(json_body().get("quantity", 0))
    except (TypeError, ValueError):
        quantity = 0
    if quantity <= 0:
        return jsonify(error="Enter a positive whole quantity."), 400
    with connect_db() as connection:
        payload = read_workspace(connection)
        part = next((item for item in payload["parts"] if int(item["id"]) == part_id), None)
        if not part:
            return jsonify(error="Spare part not found."), 404
        part["stock"] = int(part["stock"]) + quantity
        add_activity(payload, f"Received {quantity} x {part['name']}")
        write_workspace(connection, payload)
    return jsonify(part=part)


@app.get("/api/users")
@roles_required("admin")
def list_users():
    with connect_db() as connection:
        users = connection.execute("SELECT id,name,email,role,active,password_reset_required FROM users ORDER BY created_at").fetchall()
    return jsonify(users=[user_json(user) for user in users])


@app.post("/api/users/<int:user_id>/verify")
@roles_required("admin")
def verify_customer(user_id: int):
    with connect_db() as connection:
        result = connection.execute(
            "UPDATE users SET active = 1 WHERE id = ? AND role = 'customer' AND active = 0",
            (user_id,),
        )
        if result.rowcount != 1:
            return jsonify(error="Pending customer account not found."), 404
        user = connection.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return jsonify(user=user_json(user))


@app.post("/api/users/<int:user_id>/reset-password")
@roles_required("admin")
def reset_user_password(user_id: int):
    password = str(json_body().get("temporaryPassword", ""))
    if len(password) < 10:
        return jsonify(error="Temporary passwords must be at least 10 characters."), 400
    if user_id == g.user["id"]:
        return jsonify(error="Use Change password to update your own password."), 400
    with connect_db() as connection:
        user = connection.execute("SELECT id FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            return jsonify(error="Account not found."), 404
        connection.execute(
            "UPDATE users SET password_hash = ?, password_reset_required = 1 WHERE id = ?",
            (generate_password_hash(password), user_id),
        )
        connection.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))
    return jsonify(ok=True)


@app.post("/api/users")
@roles_required("admin")
def create_staff_user():
    body = json_body()
    name = str(body.get("name", "")).strip()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    role = body.get("role")
    if not name or not email or "@" not in email or role != "technician":
        return jsonify(error="Enter a name, valid email, and Technician role."), 400
    if len(password) < 10:
        return jsonify(error="Temporary passwords must be at least 10 characters."), 400
    try:
        with connect_db() as connection:
            connection.execute(
                "INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
                (name, email, generate_password_hash(password), role, now_text()),
            )
            user = connection.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        return jsonify(user=user_json(user)), 201
    except INTEGRITY_ERRORS:
        return jsonify(error="An account already exists for that email."), 409


def create_admin() -> None:
    init_db()
    name = input("Admin name: ").strip()
    email = input("Admin email: ").strip().lower()
    password = getpass("Admin password (minimum 12 characters): ")
    if not name or "@" not in email or len(password) < 12:
        raise SystemExit("A name, valid email, and password of at least 12 characters are required.")
    with connect_db() as connection:
        connection.execute(
            "INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)",
            (name, email, generate_password_hash(password), "admin", now_text()),
        )
    print("Administrator account created.")


init_db()
if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "create-admin":
        create_admin()
    else:
        app.run(host=os.environ.get("FIXTRACK_HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), debug=False)
