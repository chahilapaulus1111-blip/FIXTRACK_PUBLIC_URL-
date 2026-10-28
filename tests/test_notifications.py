import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import notifications
import server


class NotificationTests(unittest.TestCase):
    def setUp(self):
        settings = mock.patch.dict(os.environ, {"FIXTRACK_EMAIL_PROVIDER": "resend", "RESEND_API_KEY": "", "BREVO_API_KEY": "", "FIXTRACK_EMAIL_FROM": "", "FIXTRACK_PUBLIC_URL": ""})
        settings.start()
        self.addCleanup(settings.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.original_database = server.DATABASE
        server.DATABASE = Path(self.directory.name) / "notifications.sqlite3"
        server.init_db()
        server.app.config.update(TESTING=True, EMAIL_WORKER_ENABLED=False)
        self.admin = server.app.test_client()
        self.tech = server.app.test_client()
        self.customer = server.app.test_client()
        self.other = server.app.test_client()
        self.admin.post("/api/setup", json={"name": "Admin", "email": "admin@example.test", "password": "administrator-password"})
        self.admin.post("/api/users", json={"name": "Tech", "email": "tech@example.test", "password": "technician-password", "role": "technician"})
        self.tech.post("/api/login", json={"email": "tech@example.test", "password": "technician-password"})
        self.signup(self.customer, "customer@example.test")
        self.signup(self.other, "other@example.test")

    def tearDown(self):
        server.DATABASE = self.original_database
        self.directory.cleanup()

    def signup(self, client, email):
        self.assertEqual(client.post("/api/signup", json={"name": "Customer", "email": email, "password": "customer-password"}).status_code, 202)
        user = next(user for user in self.admin.get("/api/users").json["users"] if user["email"] == email)
        self.admin.post(f"/api/users/{user['id']}/verify")
        self.assertEqual(client.post("/api/login", json={"email": email, "password": "customer-password"}).status_code, 200)

    def create_job(self, email="customer@example.test"):
        response = self.tech.post("/api/jobs", json={"customer": "Customer", "customerEmail": email, "device": "Phone", "fault": "Screen", "tech": "Tech"})
        self.assertEqual(response.status_code, 201)
        return response.json["job"]["id"]

    def inbox(self, client):
        response = client.get("/api/notifications")
        self.assertEqual(response.status_code, 200)
        return response.json

    def count(self):
        with server.connect_db() as connection:
            return connection.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()["count"]

    def test_intake_quotation_decisions_and_updates_notify_all_recipients(self):
        job_id = self.create_job()
        for client in (self.admin, self.tech, self.customer):
            inbox = self.inbox(client)
            self.assertEqual(inbox["unread"], 1)
            self.assertEqual(inbox["notifications"][0]["kind"], "created")
            self.assertEqual(inbox["notifications"][0]["link"], f"/?job={job_id}")
        self.assertEqual(self.inbox(self.other)["notifications"], [])
        self.assertNotIn("emailDelivery", self.inbox(self.customer))
        self.assertNotIn("emailDelivery", self.inbox(self.tech))
        for decision in ("Declined", "Approved"):
            self.assertEqual(self.admin.post(f"/api/jobs/{job_id}/quotation", json={"quote": 300, "work": "Replace screen"}).status_code, 200)
            for client in (self.admin, self.tech, self.customer):
                self.assertEqual(self.inbox(client)["notifications"][0]["kind"], "quotation")
            response = self.customer.post(f"/api/jobs/{job_id}/decision", json={"decision": decision})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["job"]["status"], "In repair" if decision == "Approved" else "Diagnosing")
            for client in (self.admin, self.tech, self.customer):
                self.assertEqual(self.inbox(client)["notifications"][0]["title"], "Quotation " + decision.lower())
        self.assertEqual(self.tech.patch(f"/api/jobs/{job_id}", json={"status": "Ready for collection", "note": "Screen tested"}).status_code, 200)
        for client in (self.admin, self.tech, self.customer):
            self.assertIn("Screen tested", self.inbox(client)["notifications"][0]["message"])
        self.assertEqual(self.inbox(self.other)["unread"], 0)

    def test_bulk_saves_notify_without_duplicate_alerts_and_failed_saves_do_not_queue(self):
        job_id = self.create_job()
        original_count = self.count()
        workspace = self.admin.get("/api/workspace").json
        self.assertEqual(self.admin.put("/api/workspace", json=workspace).status_code, 200)
        self.assertEqual(self.count(), original_count)
        workspace = self.admin.get("/api/workspace").json
        workspace["jobs"][0].update(quote=450, work="New display", decision="Pending", status="Awaiting approval")
        self.assertEqual(self.admin.put("/api/workspace", json=workspace).status_code, 200)
        self.assertEqual(self.count(), original_count + 3)
        self.assertEqual(self.inbox(self.customer)["notifications"][0]["kind"], "quotation")
        self.assertEqual(self.admin.put("/api/workspace", json=workspace).status_code, 409)
        self.assertEqual(self.count(), original_count + 3)
        self.assertEqual(self.other.post(f"/api/jobs/{job_id}/decision", json={"decision": "Approved"}).status_code, 404)
        self.assertEqual(self.tech.post(f"/api/jobs/{job_id}/quotation", json={"quote": 10, "work": "Not allowed"}).status_code, 403)
        self.assertEqual(self.count(), original_count + 3)
        workspace = self.tech.get("/api/workspace").json
        workspace["jobs"][0]["history"].append({"text": "Diagnosis complete", "date": server.now_text()})
        self.assertEqual(self.tech.put("/api/workspace", json=workspace).status_code, 200)
        self.assertEqual(self.count(), original_count + 6)

    def test_assignment_and_parts_alerts(self):
        job_id = self.create_job()
        self.admin.post("/api/users", json={"name": "Second Tech", "email": "second-tech@example.test", "password": "technician-password", "role": "technician"})
        second = server.app.test_client()
        second.post("/api/login", json={"email": "second-tech@example.test", "password": "technician-password"})
        self.assertEqual(self.inbox(second)["unread"], 0)
        self.assertEqual(self.admin.patch(f"/api/jobs/{job_id}", json={"tech": "Second Tech"}).status_code, 200)
        self.assertEqual(self.inbox(second)["notifications"][0]["kind"], "assignment")
        old_count = self.inbox(self.tech)["unread"]
        self.admin.post(f"/api/jobs/{job_id}/quotation", json={"quote": 300, "work": "New display"})
        self.customer.post(f"/api/jobs/{job_id}/decision", json={"decision": "Approved"})
        part = self.admin.post("/api/parts", json={"name": "Display", "sku": "DISPLAY", "stock": 2, "min": 1, "cost": 20}).json["part"]
        self.assertEqual(second.post(f"/api/jobs/{job_id}/parts", json={"part": part["id"]}).status_code, 200)
        self.assertIn("Display", self.inbox(self.customer)["notifications"][0]["message"])
        self.assertIn("Display", self.inbox(second)["notifications"][0]["message"])
        self.assertEqual(self.inbox(self.tech)["unread"], old_count)

    def test_technician_intake_through_browser_workspace_save(self):
        workspace = self.tech.get("/api/workspace").json
        workspace["jobs"].append({
            "id": "FT-1049", "customer": "Customer", "customerEmail": "customer@example.test",
            "device": "Laptop", "fault": "No power", "status": "Checked in", "tech": "Tech",
            "quote": 0, "decision": "Not quoted", "parts": [],
            "history": [{"text": "Device checked in", "date": server.now_text()}],
        })
        response = self.tech.put("/api/workspace", json=workspace)
        self.assertEqual(response.status_code, 200)
        for client in (self.admin, self.tech, self.customer):
            item = self.inbox(client)["notifications"][0]
            self.assertEqual(item["kind"], "created")
            self.assertEqual(item["job_id"], "FT-1049")
        self.assertEqual(self.inbox(self.other)["notifications"], [])

    def test_read_state_is_private_and_persistent_and_signup_can_follow_intake(self):
        self.create_job(email="later@example.test")
        later = server.app.test_client()
        self.signup(later, "later@example.test")
        item = self.inbox(later)["notifications"][0]
        self.assertIn("connected to this email", item["message"])
        self.other.post("/api/notifications/read", json={"id": item["id"]})
        self.assertEqual(self.inbox(later)["unread"], 1)
        later.post("/api/notifications/read", json={"id": item["id"]})
        self.assertEqual(self.inbox(later)["unread"], 0)
        self.assertIsNotNone(self.inbox(later)["notifications"][0]["read_at"])
        self.assertEqual(self.inbox(self.admin)["unread"], 1)
        self.admin.post("/api/notifications/read", json={})
        server.init_db()
        self.assertEqual(self.inbox(self.admin)["unread"], 0)
        anonymous = server.app.test_client()
        self.assertEqual(anonymous.get("/api/notifications").status_code, 401)
        self.assertEqual(anonymous.post("/api/notifications/read").status_code, 401)
        self.assertEqual(self.customer.post("/api/notifications/retry-email").status_code, 403)

    def test_outbox_is_atomic_with_repair_save(self):
        with mock.patch.object(notifications, "queue_changes", side_effect=RuntimeError("queue failed")):
            with self.assertRaises(RuntimeError):
                self.create_job()
        self.assertEqual(self.admin.get("/api/workspace").json["jobs"], [])
        self.assertEqual(self.count(), 0)

    def test_email_configuration_failure_retry_and_success(self):
        self.create_job()
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "", "FIXTRACK_EMAIL_FROM": "", "FIXTRACK_PUBLIC_URL": ""}):
            self.assertFalse(notifications.deliver_one(server.connect_db))
            self.assertFalse(self.inbox(self.admin)["emailDelivery"]["configured"])
            self.assertEqual(self.admin.post("/api/notifications/retry-email").status_code, 409)
        configuration = {"RESEND_API_KEY": "test-key-never-send", "FIXTRACK_EMAIL_FROM": "FixTrack <workshop@example.test>", "FIXTRACK_PUBLIC_URL": "https://repairs.example.test"}
        with mock.patch.dict(os.environ, configuration), mock.patch.object(notifications, "send_email") as send:
            send.side_effect = HTTPError("https://api.resend.com/emails", 429, "Rate limit", {}, None)
            self.assertTrue(notifications.deliver_one(server.connect_db))
            self.assertEqual(self.inbox(self.admin)["emailDelivery"]["failed"], 1)
            with server.connect_db() as connection:
                failed = dict(connection.execute("SELECT * FROM notifications WHERE email_status = 'failed'").fetchone())
            self.assertEqual(failed["last_error"], "Email provider HTTP 429")
            self.assertEqual(failed["attempts"], 1)
            self.assertGreater(failed["retry_at"], failed["created_at"])
            send.side_effect = None
            self.assertEqual(self.admin.post("/api/notifications/retry-email").status_code, 200)
            while notifications.deliver_one(server.connect_db):
                pass
            status = self.inbox(self.admin)["emailDelivery"]
            self.assertEqual((status["pending"], status["failed"], status["sent"]), (0, 0, 3))
            self.assertEqual(self.inbox(self.customer)["unread"], 1)

    def test_email_payload_has_private_recipient_safe_origin_and_stable_retry_key(self):
        self.create_job()
        with server.connect_db() as connection:
            item = dict(connection.execute("SELECT * FROM notifications WHERE recipient_email = ?", ("customer@example.test",)).fetchone())
        config = {"RESEND_API_KEY": "test-key-never-send", "FIXTRACK_EMAIL_FROM": "workshop@example.test", "FIXTRACK_PUBLIC_URL": "https://repairs.example.test"}
        with mock.patch.dict(os.environ, config), mock.patch.object(notifications, "urlopen") as transport:
            transport.return_value.__enter__.return_value.read.return_value = b'{"id":"test-provider-id"}'
            notifications.send_email(item)
            notifications.send_email(item)
            requests = [call.args[0] for call in transport.call_args_list]
            self.assertEqual(requests[0].get_header("Idempotency-key"), requests[1].get_header("Idempotency-key"))
            payload = json.loads(requests[0].data)
            self.assertEqual(payload["to"], ["customer@example.test"])
            self.assertIn("https://repairs.example.test/?job=", payload["text"])
            self.assertNotIn("admin@example.test", payload["text"])
            for bad_origin in ("https://attacker.test/?redirect=", "https://user:password@example.test", "http://public.example.test", "https://example.test/path", "https://[invalid", "https://example.test:invalid", "https://bad host.test"):
                with mock.patch.dict(os.environ, {"FIXTRACK_PUBLIC_URL": bad_origin}):
                    self.assertFalse(notifications.email_configuration())

    def test_outbox_claim_lease_survives_restart_and_retry_limit(self):
        self.create_job()
        with server.connect_db() as connection:
            item = dict(connection.execute("SELECT * FROM notifications LIMIT 1").fetchone())
            connection.execute("UPDATE notifications SET email_status = 'sent'")
            connection.execute("UPDATE notifications SET email_status = 'sending', attempts = 1, retry_at = ? WHERE id = ?", (notifications.timestamp(300), item["id"]))
        config = {"RESEND_API_KEY": "test-key-never-send", "FIXTRACK_EMAIL_FROM": "workshop@example.test", "FIXTRACK_PUBLIC_URL": "https://repairs.example.test"}
        with mock.patch.dict(os.environ, config), mock.patch.object(notifications, "send_email") as send:
            self.assertFalse(notifications.deliver_one(server.connect_db))
            send.assert_not_called()
            with server.connect_db() as connection:
                connection.execute("UPDATE notifications SET retry_at = ? WHERE id = ?", (notifications.timestamp(-1), item["id"]))
            server.init_db()

            def while_sending(_):
                self.assertFalse(notifications.deliver_one(server.connect_db))

            send.side_effect = while_sending
            self.assertTrue(notifications.deliver_one(server.connect_db))
            send.assert_called_once()
            self.assertFalse(notifications.deliver_one(server.connect_db))
            with server.connect_db() as connection:
                connection.execute("UPDATE notifications SET email_status = 'failed', attempts = 8, retry_at = ? WHERE id = ?", (notifications.timestamp(-1), item["id"]))
            self.assertFalse(notifications.deliver_one(server.connect_db))

    def test_brevo_sends_from_verified_mailbox_with_tracking_and_private_recipients(self):
        self.create_job()
        config = {"FIXTRACK_EMAIL_PROVIDER": "brevo", "BREVO_API_KEY": "test-brevo-key-never-send", "FIXTRACK_EMAIL_FROM": "FixTrack <workshop@gmail.com>", "FIXTRACK_PUBLIC_URL": "https://workshop.onrender.com"}
        with mock.patch.dict(os.environ, config), mock.patch.object(notifications, "urlopen") as transport:
            transport.return_value.__enter__.return_value.read.return_value = b'{"messageId":"test-brevo-id"}'
            self.assertTrue(notifications.email_configuration())
            self.assertTrue(notifications.deliver_one(server.connect_db))
            outgoing = transport.call_args.args[0]
            self.assertEqual(outgoing.full_url, "https://api.brevo.com/v3/smtp/email")
            self.assertEqual(outgoing.get_header("Api-key"), "test-brevo-key-never-send")
            self.assertIsNone(outgoing.get_header("Authorization"))
            payload = json.loads(outgoing.data)
            self.assertEqual(payload["sender"], {"name": "FixTrack", "email": "workshop@gmail.com"})
            self.assertEqual(payload["replyTo"], payload["sender"])
            self.assertEqual(len(payload["to"]), 1)
            self.assertIn("https://workshop.onrender.com/?job=", payload["textContent"])
            with server.connect_db() as connection:
                sent = dict(connection.execute("SELECT * FROM notifications WHERE email_status = 'sent'").fetchone())
            self.assertEqual(payload["to"], [{"email": sent["recipient_email"]}])
            self.assertEqual(payload["headers"]["idempotencyKey"], sent["id"])
            notifications.send_email(sent)
            self.assertEqual(json.loads(transport.call_args.args[0].data)["headers"], payload["headers"])
            status = self.inbox(self.admin)["emailDelivery"]
            self.assertEqual((status["provider"], status["sent"]), ("brevo", 1))

    def test_selected_provider_must_have_its_own_key_and_valid_acceptance(self):
        self.create_job()
        with server.connect_db() as connection:
            item = dict(connection.execute("SELECT * FROM notifications LIMIT 1").fetchone())
        config = {"FIXTRACK_EMAIL_PROVIDER": "brevo", "RESEND_API_KEY": "resend-key-must-not-be-used", "FIXTRACK_EMAIL_FROM": "workshop@gmail.com", "FIXTRACK_PUBLIC_URL": "https://workshop.onrender.com"}
        with mock.patch.dict(os.environ, config), mock.patch.object(notifications, "urlopen") as transport:
            self.assertFalse(notifications.email_configuration())
            self.assertFalse(notifications.deliver_one(server.connect_db))
            with self.assertRaises(ValueError):
                notifications.send_email(item)
            transport.assert_not_called()
            with mock.patch.dict(os.environ, {"BREVO_API_KEY": "brevo-key-never-send"}):
                transport.return_value.__enter__.return_value.read.return_value = b'{"id":"wrong-provider-response"}'
                with self.assertRaises(ValueError):
                    notifications.send_email(item)
            with mock.patch.dict(os.environ, {"FIXTRACK_EMAIL_PROVIDER": "unknown"}):
                self.assertFalse(notifications.email_configuration())


if __name__ == "__main__":
    unittest.main()
