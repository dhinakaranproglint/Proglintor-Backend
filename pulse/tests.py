import time

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from .models import Asset, DailyUsage, WeekendAlert


def now_ms():
    return int(time.time() * 1000)


class AdminApiTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True)
        User.objects.create_user("emp", password="s3cret-pass", is_staff=False)
        self.api = APIClient()

    def login(self, user="admin"):
        r = self.api.post("/api/admin/login", {"username": user, "password": "s3cret-pass"}, format="json")
        if r.status_code == 200:
            self.api.credentials(HTTP_AUTHORIZATION=f"Token {r.json()['token']}")
        return r

    def test_login_and_permissions(self):
        self.assertEqual(self.api.get("/api/admin/assets").status_code, 401)
        self.assertEqual(self.api.post("/api/admin/login", {"username": "admin", "password": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.login("emp").status_code, 403, "non-staff users cannot use the console")
        self.assertEqual(self.login().status_code, 200)
        self.assertEqual(self.api.get("/api/admin/me").json()["username"], "admin")
        self.api.post("/api/admin/logout")
        self.assertEqual(self.api.get("/api/admin/me").status_code, 401, "token revoked on logout")

    def test_create_validate_update_delete_asset(self):
        self.login()
        bad = self.api.post("/api/admin/assets", {"assetId": "PGL 1", "employeeName": "A", "employeeEmail": "a@b.co"}, format="json")
        self.assertEqual(bad.status_code, 400)
        bad = self.api.post("/api/admin/assets", {"assetId": "PGL-1", "employeeName": "A", "employeeEmail": "not-an-email"}, format="json")
        self.assertIn("email", bad.json()["error"])
        r = self.api.post("/api/admin/assets", {"assetId": "PGL-LT-0042", "employeeName": "Ravi Kumar", "employeeEmail": "ravi@proglint.com",
                                                 "department": "Engineering", "location": "Chennai"}, format="json")
        self.assertEqual(r.status_code, 201)
        body = r.json()
        self.assertRegex(body["enrollCode"], r"^[A-Z2-9]{4}-[A-Z2-9]{4}$")
        self.assertEqual(body["state"], "not-enrolled")
        dup = self.api.post("/api/admin/assets", {"assetId": "pgl-lt-0042", "employeeName": "X", "employeeEmail": "x@y.co"}, format="json")
        self.assertIn("already exists", dup.json()["error"])
        r = self.api.patch("/api/admin/assets/PGL-LT-0042", {"department": "Platform"}, format="json")
        self.assertEqual(r.json()["department"], "Platform")
        self.assertEqual(r.json()["employeeName"], "Ravi Kumar", "partial update keeps other fields")
        self.assertEqual(self.api.delete("/api/admin/assets/PGL-LT-0042").status_code, 204)
        self.assertFalse(Asset.objects.exists())

    def test_policy_roundtrip(self):
        self.login()
        p = self.api.get("/api/admin/policy").json()
        self.assertTrue(p["policy"]["weekendCheck"])
        r = self.api.put("/api/admin/policy", {**p["policy"], "weekendDays": [5, 6, 6, 9], "devTools": ["Xcode"]}, format="json")
        self.assertEqual(r.json()["weekendDays"], [5, 6])
        self.assertEqual(self.api.get("/api/admin/policy").json()["policy"]["devTools"], ["Xcode"])


class AgentFlowTests(TestCase):
    """Same payloads the Electron agent sends (proglint-pulse/src/agent/sync.js)."""

    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True)
        self.admin = APIClient()
        token = self.admin.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.admin.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.code = self.admin.post("/api/admin/assets", {"assetId": "PAP080", "employeeName": "Dhinakaran", "employeeEmail": "dhina@proglint.com"},
                                    format="json").json()["enrollCode"]
        self.agent = APIClient()

    def enroll(self, code=None):
        return self.agent.post("/api/agent/enroll", {"code": code or self.code, "hostname": "dhina-mbp", "os": "macOS 24.6.0",
                                                     "platform": "darwin", "agentVersion": "1.0.0"}, format="json")

    def test_full_agent_flow(self):
        self.assertEqual(self.enroll("WRNG-CODE").status_code, 404)
        r = self.enroll(self.code.lower().replace("-", ""))  # case/format tolerant
        self.assertEqual(r.status_code, 200)
        token = r.json()["token"]
        self.assertEqual(r.json()["asset"]["employeeName"], "Dhinakaran")
        self.assertIn("devTools", r.json()["policy"])
        self.assertEqual(self.enroll().status_code, 404, "code is single use")

        self.assertEqual(self.agent.post("/api/agent/heartbeat", {}, format="json").status_code, 401)
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        hb = self.agent.post("/api/agent/heartbeat", {"state": "tracking", "current": {"id": "com.microsoft.VSCode", "name": "Code"},
                                                       "sessionStart": now_ms() - 60000, "todaySeconds": 540,
                                                       "runningApps": [{"id": "com.microsoft.VSCode", "name": "Code"}, {"id": "com.apple.Safari", "name": "Safari"}]},
                             format="json")
        self.assertEqual(hb.status_code, 200)
        self.assertIn("policy", hb.json())

        today = timezone.localdate().isoformat()
        day = [{"id": "com.microsoft.VSCode", "name": "Code", "seconds": 540, "sessions": 3, "activations": 4, "lastActive": now_ms()}]
        self.assertEqual(self.agent.post("/api/agent/usage", {"days": {today: day}}, format="json").status_code, 200)
        self.agent.post("/api/agent/usage", {"days": {today: day}}, format="json")  # resend: replaced, not doubled
        self.assertEqual(DailyUsage.objects.get().seconds, 540)
        self.agent.post("/api/agent/installed", {"apps": [{"id": "com.microsoft.VSCode", "name": "Visual Studio Code", "version": "1.140.0"}]}, format="json")
        alert = {"uid": "a1", "createdAt": now_ms(), "day": today, "appId": "com.microsoft.VSCode", "appName": "Code",
                 "reason": "Project deadline", "details": "Release prep", "duration": "1–3 hours", "approver": "Karthik"}
        r = self.agent.post("/api/agent/alerts", {"alerts": [alert, alert]}, format="json")
        self.assertEqual(r.json()["received"], ["a1", "a1"])
        self.assertEqual(WeekendAlert.objects.count(), 1, "alerts are idempotent by uid")

        dash = self.admin.get("/api/admin/dashboard").json()
        self.assertEqual(dash["kpis"]["online"], 1)
        self.assertEqual(dash["kpis"]["newAlerts"], 1)
        self.assertEqual(dash["topApps"][0]["name"], "Code")
        self.assertEqual(len(dash["daily"]), 7)
        detail = self.admin.get("/api/admin/assets/PAP080").json()
        self.assertEqual(detail["asset"]["current"]["name"], "Code")
        self.assertEqual([a["name"] for a in detail["asset"]["runningApps"]], ["Code", "Safari"])
        self.assertEqual(detail["installed"]["apps"][0]["version"], "1.140.0")
        self.assertEqual(detail["todayApps"][0]["seconds"], 540)
        self.assertEqual(detail["alerts"][0]["employeeName"], "Dhinakaran")

        r = self.admin.patch("/api/admin/alerts/a1", {"status": "reviewed", "adminNote": "OK'd"}, format="json")
        self.assertEqual(r.json()["reviewedBy"], "admin")
        self.assertEqual(len(self.admin.get("/api/admin/alerts?status=new").json()), 0)
        self.assertIn("PAP080", self.admin.get("/api/admin/export.csv").content.decode())

        # A new code disconnects the laptop: the agent gets 401 and asks for a new code.
        self.admin.post("/api/admin/assets/PAP080/reset-code")
        self.assertEqual(self.agent.post("/api/agent/heartbeat", {}, format="json").status_code, 401)

    def test_enroll_rate_limit(self):
        for _ in range(10):
            self.enroll("ZZZZ-ZZZZ")
        self.assertEqual(self.enroll().status_code, 429)
