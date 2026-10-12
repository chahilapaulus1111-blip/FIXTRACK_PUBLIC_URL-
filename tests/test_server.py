import tempfile
import unittest
from pathlib import Path

import server


class BackendWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        server.DATABASE = Path(self.temporary_directory.name) / "test.sqlite3"
        server.init_db()
        server.app.config.update(TESTING=True)
        self.admin = server.app.test_client()
        self.customer = server.app.test_client()
        self.technician = server.app.test_client()

    def tearDown(self):
        self.temporary_directory.cleanup()

    def test_server_backed_roles_and_repair_workflow(self):
        self.assertTrue(self.admin.get("/api/setup-status").json["needsAdmin"])
        setup = self.admin.post("/api/setup", json={
            "name": "Test Admin",
            "email": "admin@example.test",
            "password": "correct-horse-test-password",
        })
        self.assertEqual(setup.status_code, 201)
        self.assertEqual(setup.json["user"]["role"], "admin")
        self.assertEqual(self.admin.post("/api/setup", json={}).status_code, 409)

        job_response = self.admin.post("/api/jobs", json={
            "customer": "Test Customer",
            "customerEmail": "customer@example.test",
            "device": "Test phone",
            "fault": "Broken screen",
        })
        self.assertEqual(job_response.status_code, 201)
        job_id = job_response.json["job"]["id"]
        with server.connect_db() as connection:
            workspace = server.read_workspace(connection)
            workspace["jobs"][0]["tech"] = "David"
            server.write_workspace(connection, workspace)

        part_response = self.admin.post("/api/parts", json={
            "name": "Test display", "sku": "TEST-DISPLAY", "stock": 2, "min": 1, "cost": 25,
        })
        self.assertEqual(part_response.status_code, 201)
        part_id = part_response.json["part"]["id"]

        staff_response = self.admin.post("/api/users", json={
            "name": "Test Technician",
            "email": "tech@example.test",
            "password": "temporary-test-password",
            "role": "technician",
        })
        self.assertEqual(staff_response.status_code, 201)
        technician_id = staff_response.json["user"]["id"]
        self.assertEqual(self.admin.get("/api/workspace").json["technicians"], [{"name": "Test Technician"}])
        legacy_workspace = self.admin.get("/api/workspace").json
        self.assertEqual(legacy_workspace["jobs"][0]["tech"], "David")
        self.assertEqual(self.admin.put("/api/workspace", json=legacy_workspace).status_code, 200)
        self.assertEqual(self.admin.patch(f"/api/jobs/{job_id}", json={"tech": "Unknown Technician"}).status_code, 400)
        self.assertEqual(self.admin.patch(f"/api/jobs/{job_id}", json={"tech": "Test Technician"}).status_code, 200)
        self.assertEqual(self.technician.post("/api/login", json={
            "email": "tech@example.test", "password": "temporary-test-password",
        }).status_code, 200)
        self.assertEqual(self.technician.get("/api/workspace").status_code, 200)
        self.assertEqual(self.admin.post(f"/api/users/{setup.json['user']['id']}/reset-password", json={
            "temporaryPassword": "AdminResetPassword!42",
        }).status_code, 400)
        self.assertEqual(self.admin.post(f"/api/users/{technician_id}/reset-password", json={
            "temporaryPassword": "AdminResetPassword!42",
        }).status_code, 200)
        self.assertEqual(self.technician.get("/api/workspace").status_code, 401)
        self.assertEqual(self.technician.post("/api/login", json={
            "email": "tech@example.test", "password": "temporary-test-password",
        }).status_code, 401)
        self.assertEqual(self.technician.post("/api/login", json={
            "email": "tech@example.test", "password": "AdminResetPassword!42",
        }).status_code, 200)
        self.assertEqual(self.technician.get("/api/workspace").status_code, 428)
        self.assertTrue(self.technician.get("/api/session").json["user"]["passwordResetRequired"])
        self.assertEqual(self.technician.post("/api/password/required-change", json={
            "newPassword": "NewTechnicianPassword!42",
        }).status_code, 200)
        self.assertEqual(self.technician.get("/api/workspace").status_code, 200)
        self.assertEqual(self.technician.post("/api/password/change", json={
            "currentPassword": "incorrect-password", "newPassword": "AnotherTechnicianPassword!42",
        }).status_code, 401)
        self.assertEqual(self.technician.post("/api/password/change", json={
            "currentPassword": "NewTechnicianPassword!42", "newPassword": "AnotherTechnicianPassword!42",
        }).status_code, 200)
        self.assertEqual(self.technician.post("/api/login", json={
            "email": "tech@example.test", "password": "AnotherTechnicianPassword!42",
        }).status_code, 200)

        signup = self.customer.post("/api/signup", json={
            "name": "Test Customer",
            "email": "customer@example.test",
            "password": "customer-test-password",
            "role": "admin",
        })
        self.assertEqual(signup.status_code, 202)
        self.assertEqual(self.customer.post("/api/login", json={
            "email": "customer@example.test", "password": "customer-test-password",
        }).status_code, 403)
        customer_user = next(user for user in self.admin.get("/api/users").json["users"] if user["email"] == "customer@example.test")
        self.assertEqual(self.admin.post(f"/api/users/{customer_user['id']}/verify").status_code, 200)
        self.assertEqual(self.customer.post("/api/login", json={
            "email": "customer@example.test", "password": "customer-test-password",
        }).status_code, 200)
        customer_workspace = self.customer.get("/api/workspace").json
        self.assertEqual([job["id"] for job in customer_workspace["jobs"]], [job_id])
        self.assertEqual(customer_workspace["parts"], [])
        self.assertEqual(self.customer.post("/api/jobs", json={}).status_code, 403)

        self.assertEqual(self.admin.post(f"/api/jobs/{job_id}/quotation", json={
            "quote": 300, "work": "Replace display",
        }).status_code, 200)
        self.assertEqual(self.customer.post(f"/api/jobs/{job_id}/decision", json={"decision": "Approved"}).status_code, 200)

        self.assertEqual(self.technician.post(f"/api/jobs/{job_id}/quotation", json={
            "quote": 500, "work": "Unauthorized price change",
        }).status_code, 403)
        self.assertEqual(self.technician.patch(f"/api/jobs/{job_id}", json={"status": "In repair"}).status_code, 200)
        used = self.technician.post(f"/api/jobs/{job_id}/parts", json={"part": part_id})
        self.assertEqual(used.status_code, 200)
        used_again = self.technician.post(f"/api/jobs/{job_id}/parts", json={"part": part_id})
        self.assertEqual(used_again.status_code, 200)

        restricted_workspace = self.technician.get("/api/workspace").json
        restricted_workspace["parts"][0]["cost"] = 1
        self.assertEqual(self.technician.put("/api/workspace", json=restricted_workspace).status_code, 403)
        self.assertEqual(self.admin.post(f"/api/jobs/{job_id}/decision", json={"decision": "Approved"}).status_code, 403)

        fresh_admin = server.app.test_client()
        self.assertEqual(fresh_admin.post("/api/login", json={
            "email": "admin@example.test", "password": "correct-horse-test-password",
        }).status_code, 200)
        persisted = fresh_admin.get("/api/workspace").json
        self.assertEqual(persisted["jobs"][0]["status"], "In repair")
        self.assertEqual(persisted["parts"][0]["stock"], 0)


if __name__ == "__main__":
    unittest.main()
