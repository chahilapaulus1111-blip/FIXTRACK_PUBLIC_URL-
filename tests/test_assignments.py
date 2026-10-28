import copy
import tempfile
import unittest
from pathlib import Path

import server


class TechnicianAssignmentTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.original_database = server.DATABASE
        server.DATABASE = Path(self.directory.name) / "assignments.sqlite3"
        server.init_db()
        server.app.config.update(TESTING=True, EMAIL_WORKER_ENABLED=False)
        self.admin = server.app.test_client()
        self.first = server.app.test_client()
        self.second = server.app.test_client()
        self.admin.post("/api/setup", json={"name": "Admin", "email": "admin@example.test", "password": "administrator-password"})
        for name, email, client in (
            ("First Technician", "first@example.test", self.first),
            ("Second Technician", "second@example.test", self.second),
        ):
            created = self.admin.post("/api/users", json={"name": name, "email": email, "password": "technician-password", "role": "technician"})
            self.assertEqual(created.status_code, 201)
            self.assertEqual(client.post("/api/login", json={"email": email, "password": "technician-password"}).status_code, 200)
        self.own = self.create_job("First Technician")
        self.other = self.create_job("Second Technician")
        self.unassigned = self.create_job("Unassigned")

    def tearDown(self):
        server.DATABASE = self.original_database
        self.directory.cleanup()

    def create_job(self, technician, client=None):
        response = (client or self.admin).post("/api/jobs", json={
            "customer": "Customer", "customerEmail": "customer@example.test",
            "device": "Phone", "fault": "Broken screen", "tech": technician,
        })
        self.assertEqual(response.status_code, 201)
        return response.json["job"]["id"]

    def state(self):
        workspace = self.admin.get("/api/workspace").json
        with server.connect_db() as connection:
            notifications = connection.execute("SELECT COUNT(*) AS count FROM notifications").fetchone()["count"]
        return workspace, notifications

    def approve_jobs(self):
        customer = server.app.test_client()
        customer.post("/api/signup", json={"name": "Customer", "email": "customer@example.test", "password": "customer-password"})
        user = next(user for user in self.admin.get("/api/users").json["users"] if user["role"] == "customer")
        self.admin.post(f"/api/users/{user['id']}/verify")
        customer.post("/api/login", json={"email": "customer@example.test", "password": "customer-password"})
        for job_id in (self.own, self.other, self.unassigned):
            self.assertEqual(self.admin.post(f"/api/jobs/{job_id}/quotation", json={"quote": 300, "work": "Replace screen"}).status_code, 200)
            self.assertEqual(customer.post(f"/api/jobs/{job_id}/decision", json={"decision": "Approved"}).status_code, 200)

    def test_edit_permissions_allow_read_only_access_to_other_jobs(self):
        first = self.first.get("/api/workspace").json
        second = self.second.get("/api/workspace").json
        self.assertEqual(first["editableJobIds"], [self.own])
        self.assertEqual(second["editableJobIds"], [self.other])
        self.assertEqual({job["id"] for job in first["jobs"]}, {self.own, self.other, self.unassigned})
        self.assertEqual(set(self.admin.get("/api/workspace").json["editableJobIds"]), {self.own, self.other, self.unassigned})

    def test_direct_updates_block_other_jobs_unassigned_jobs_and_reassignment(self):
        before = self.state()
        for job_id in (self.other, self.unassigned):
            for change in ({"status": "Diagnosing"}, {"note": "Unauthorized note"}, {"tech": "First Technician", "status": "Diagnosing"}):
                response = self.first.patch(f"/api/jobs/{job_id}", json=change)
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.state(), before)
        for assignee in ("Second Technician", "Unassigned"):
            self.assertEqual(self.first.patch(f"/api/jobs/{self.own}", json={"tech": assignee}).status_code, 403)
            self.assertEqual(self.state(), before)
        self.assertEqual(self.first.patch(f"/api/jobs/{self.own}", json={"status": "Diagnosing", "note": "Assigned technician diagnosis"}).status_code, 200)

    def test_parts_cannot_be_used_on_another_technicians_job(self):
        self.approve_jobs()
        part = self.admin.post("/api/parts", json={"name": "Display", "sku": "DISPLAY", "stock": 2, "min": 0, "cost": 30}).json["part"]
        before = self.state()
        for job_id in (self.other, self.unassigned):
            self.assertEqual(self.first.post(f"/api/jobs/{job_id}/parts", json={"part": part["id"]}).status_code, 403)
            self.assertEqual(self.state(), before)
        self.assertEqual(self.first.post(f"/api/jobs/{self.own}/parts", json={"part": part["id"]}).status_code, 200)
        self.assertEqual(self.second.post(f"/api/jobs/{self.other}/parts", json={"part": part["id"]}).status_code, 200)
        self.assertEqual(self.admin.get("/api/workspace").json["parts"][0]["stock"], 0)

    def test_workspace_cannot_change_other_jobs_or_claim_them(self):
        self.approve_jobs()
        self.admin.post("/api/parts", json={"name": "Display", "sku": "DISPLAY", "stock": 2, "min": 0, "cost": 30})
        before = self.state()
        for job_id in (self.other, self.unassigned):
            for field in ("status", "history", "parts", "tech", "customer"):
                incoming = self.first.get("/api/workspace").json
                job = next(job for job in incoming["jobs"] if job["id"] == job_id)
                # A mixed save must roll back the permitted change as well.
                own = next(job for job in incoming["jobs"] if job["id"] == self.own)
                own["history"].append({"text": "Allowed note in a rejected batch", "date": server.now_text()})
                if field == "status":
                    job["status"] = "Ready for collection"
                elif field == "history":
                    job["history"].append({"text": "Not allowed", "date": server.now_text()})
                elif field == "parts":
                    job["parts"].append({"name": "Display", "qty": 1})
                    incoming["parts"][0]["stock"] -= 1
                else:
                    job[field] = "First Technician" if field == "tech" else "Changed customer"
                self.assertEqual(self.first.put("/api/workspace", json=incoming).status_code, 403)
                self.assertEqual(self.state(), before)
        incoming = self.first.get("/api/workspace").json
        next(job for job in incoming["jobs"] if job["id"] == self.own)["tech"] = "Second Technician"
        self.assertEqual(self.first.put("/api/workspace", json=incoming).status_code, 403)
        self.assertEqual(self.state(), before)

    def test_workspace_saves_own_changes_with_other_jobs_unchanged(self):
        incoming = self.first.get("/api/workspace").json
        other_jobs = copy.deepcopy([job for job in incoming["jobs"] if job["id"] != self.own])
        job = next(job for job in incoming["jobs"] if job["id"] == self.own)
        job["status"] = "Diagnosing"
        job["history"].append({"text": "Assigned technician note", "date": server.now_text()})
        response = self.first.put("/api/workspace", json=incoming)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["editableJobIds"], [self.own])
        self.assertEqual([job for job in response.json["jobs"] if job["id"] != self.own], other_jobs)
        self.assertEqual(self.first.put("/api/workspace", json=self.first.get("/api/workspace").json).status_code, 200)

    def test_admin_reassignment_immediately_revokes_old_technician_access(self):
        stale = self.first.get("/api/workspace").json
        next(job for job in stale["jobs"] if job["id"] == self.own)["status"] = "Diagnosing"
        self.assertEqual(self.admin.patch(f"/api/jobs/{self.own}", json={"tech": "Second Technician"}).status_code, 200)
        after_reassignment = self.state()
        self.assertEqual(self.first.put("/api/workspace", json=stale).status_code, 409)
        self.assertEqual(self.first.patch(f"/api/jobs/{self.own}", json={"note": "Old owner"}).status_code, 403)
        fresh = self.first.get("/api/workspace").json
        self.assertNotIn(self.own, fresh["editableJobIds"])
        next(job for job in fresh["jobs"] if job["id"] == self.own)["status"] = "Diagnosing"
        self.assertEqual(self.first.put("/api/workspace", json=fresh).status_code, 403)
        self.assertEqual(self.state(), after_reassignment)
        self.assertEqual(self.second.patch(f"/api/jobs/{self.own}", json={"note": "New owner"}).status_code, 200)
        self.assertEqual(self.admin.patch(f"/api/jobs/{self.unassigned}", json={"status": "Diagnosing"}).status_code, 200)

    def test_new_intakes_remain_allowed_but_do_not_grant_the_creator_edit_access(self):
        job_id = self.create_job("Second Technician", client=self.first)
        self.assertEqual(self.first.patch(f"/api/jobs/{job_id}", json={"status": "Diagnosing"}).status_code, 403)
        self.assertEqual(self.second.patch(f"/api/jobs/{job_id}", json={"status": "Diagnosing"}).status_code, 200)

    def test_duplicate_technician_names_do_not_grant_access(self):
        duplicate = {"name": "first technician", "email": "duplicate@example.test", "password": "technician-password", "role": "technician"}
        self.assertEqual(self.admin.post("/api/users", json=duplicate).status_code, 409)
        self.assertEqual(self.admin.post("/api/users", json={**duplicate, "name": "Unassigned"}).status_code, 400)
        # Imported legacy duplicates also fail closed, rather than sharing a job's access.
        with server.connect_db() as connection:
            existing = connection.execute("SELECT * FROM users WHERE email = ?", ("first@example.test",)).fetchone()
            connection.execute("INSERT INTO users(name,email,password_hash,role,created_at) VALUES(?,?,?,?,?)", (existing["name"], "duplicate@example.test", existing["password_hash"], "technician", server.now_text()))
        duplicate_client = server.app.test_client()
        self.assertEqual(duplicate_client.post("/api/login", json={"email": "duplicate@example.test", "password": "technician-password"}).status_code, 200)
        for client in (self.first, duplicate_client):
            self.assertEqual(client.get("/api/workspace").json["editableJobIds"], [])
            self.assertEqual(client.patch(f"/api/jobs/{self.own}", json={"note": "Ambiguous owner"}).status_code, 403)
        self.assertEqual(self.admin.patch(f"/api/jobs/{self.own}", json={"status": "Diagnosing"}).status_code, 200)


if __name__ == "__main__":
    unittest.main()
