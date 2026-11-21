import re
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
        r = self.agent.post("/api/agent/heartbeat", {}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "device_removed")

    def test_icons_upload_and_admin_fetch(self):
        import base64
        token = self.enroll().json()["token"]
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        png = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-icon").decode()
        r = self.agent.post("/api/agent/icons/missing", {"ids": ["com.microsoft.VSCode", "com.apple.Safari"]}, format="json")
        self.assertEqual(r.json()["missing"], ["com.microsoft.VSCode", "com.apple.Safari"])
        r = self.agent.post("/api/agent/icons", {"icons": [
            {"id": "com.microsoft.VSCode", "data": f"data:image/png;base64,{png}"},
            {"id": "bad", "data": "data:text/html;base64,PGgxPg=="},  # rejected: not an image
        ]}, format="json")
        self.assertEqual(r.json()["saved"], 1)
        r = self.agent.post("/api/agent/icons/missing", {"ids": ["com.microsoft.VSCode", "com.apple.Safari"]}, format="json")
        self.assertEqual(r.json()["missing"], ["com.apple.Safari"])
        icons = self.admin.post("/api/admin/icons", {"ids": ["com.microsoft.VSCode", "com.apple.Safari"]}, format="json").json()["icons"]
        self.assertEqual(list(icons), ["com.microsoft.VSCode"])
        self.assertTrue(icons["com.microsoft.VSCode"].startswith("data:image/png;base64,"))
        self.assertEqual(APIClient().post("/api/admin/icons", {"ids": ["x"]}, format="json").status_code, 401)

    def test_deleted_asset_is_reported_to_the_agent_explicitly(self):
        token = self.enroll().json()["token"]
        self.agent.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertEqual(self.agent.post("/api/agent/heartbeat", {}, format="json").status_code, 200)
        self.assertEqual(self.admin.delete("/api/admin/assets/PAP080").status_code, 204)
        r = self.agent.post("/api/agent/heartbeat", {"state": "tracking"}, format="json")
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["code"], "device_removed")
        self.assertEqual(self.agent.post("/api/agent/usage", {"days": {}}, format="json").json()["code"], "device_removed")
        # a request with no credentials at all is a different problem, not "removed"
        self.assertEqual(APIClient().post("/api/agent/heartbeat", {}, format="json").json()["code"], "unauthorized")

    def test_enroll_returns_asset_details_for_the_agent(self):
        r = self.enroll().json()["asset"]
        self.assertEqual(r["deviceType"], "Laptop")
        self.assertIsNotNone(r["enrolledAt"])

    def test_enroll_rate_limit(self):
        for _ in range(10):
            self.enroll("ZZZZ-ZZZZ")
        self.assertEqual(self.enroll().status_code, 429)


class AuthFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        self.api = APIClient()

    def test_first_admin_setup_only_locally_and_only_once(self):
        r = self.api.post("/api/admin/login", {"login": "anyone", "password": "x"}, format="json")
        self.assertTrue(r.json().get("needsSetup"), "login explains that no admin exists yet")
        self.assertEqual(self.api.get("/api/admin/setup").json(), {"needsSetup": True, "allowed": True})
        remote = self.api.post("/api/admin/setup", {"username": "x", "email": "x@proglint.com", "password": "Str0ng-Pass-9"}, format="json", REMOTE_ADDR="10.0.0.5")
        self.assertEqual(remote.status_code, 403, "not allowed from another computer")
        weak = self.api.post("/api/admin/setup", {"username": "dhina", "email": "dhina@proglint.com", "password": "123"}, format="json")
        self.assertEqual(weak.json()["field"], "password")
        r = self.api.post("/api/admin/setup", {"name": "Dhinakaran C", "username": "dhina", "email": "dhina@proglint.com", "password": "Str0ng-Pass-9"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(r.json()["user"]["name"], "Dhinakaran C")
        self.assertEqual(self.api.post("/api/admin/setup", {"username": "y", "email": "y@proglint.com", "password": "Str0ng-Pass-9"}, format="json").status_code, 409)
        self.assertFalse(self.api.get("/api/admin/setup").json()["needsSetup"])
        # Sign in with the e-mail address instead of the username.
        self.assertEqual(self.api.post("/api/admin/login", {"login": "DHINA@proglint.com", "password": "Str0ng-Pass-9"}, format="json").status_code, 200)

    def test_forgot_and_reset_password(self):
        from django.core import mail
        User.objects.create_user("dhina", email="dhina@proglint.com", password="Old-Pass-123x", is_staff=True)
        old_token = self.api.post("/api/admin/login", {"login": "dhina", "password": "Old-Pass-123x"}, format="json").json()["token"]

        unknown = self.api.post("/api/admin/password/forgot", {"login": "nobody@proglint.com"}, format="json")
        known = self.api.post("/api/admin/password/forgot", {"login": "dhina@proglint.com"}, format="json")
        self.assertEqual(unknown.json()["message"], known.json()["message"], "same answer: doesn't reveal which accounts exist")
        self.assertEqual(len(mail.outbox), 1)
        code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)

        wrong = "000000" if code != "000000" else "111111"
        r = self.api.post("/api/admin/password/reset", {"login": "dhina", "code": wrong, "password": "New-Pass-456y"}, format="json")
        self.assertIn("4 attempts left", r.json()["error"])
        r = self.api.post("/api/admin/password/reset", {"login": "dhina", "code": code, "password": "short"}, format="json")
        self.assertEqual(r.json()["field"], "password")
        r = self.api.post("/api/admin/password/reset", {"login": "dhina", "code": f"{code[:3]} {code[3:]}", "password": "New-Pass-456y"}, format="json")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.api.post("/api/admin/password/reset", {"login": "dhina", "code": code, "password": "Other-Pass-789z"}, format="json").status_code, 400, "code is single use")
        self.assertEqual(self.api.post("/api/admin/login", {"login": "dhina", "password": "New-Pass-456y"}, format="json").status_code, 200)
        self.api.credentials(HTTP_AUTHORIZATION=f"Token {old_token}")
        self.assertEqual(self.api.get("/api/admin/me").status_code, 401, "old sessions are signed out after a reset")

    def test_expired_code(self):
        from datetime import timedelta
        from django.core import mail
        from .models import PasswordResetCode
        User.objects.create_user("dhina", email="dhina@proglint.com", password="Old-Pass-123x", is_staff=True)
        self.api.post("/api/admin/password/forgot", {"login": "dhina"}, format="json")
        code = re.search(r"\b(\d{6})\b", mail.outbox[0].body).group(1)
        PasswordResetCode.objects.update(expires_at=timezone.now() - timedelta(minutes=1))
        r = self.api.post("/api/admin/password/reset", {"login": "dhina", "code": code, "password": "New-Pass-456y"}, format="json")
        self.assertIn("expired", r.json()["error"])

    def test_setup_with_email_only(self):
        r = self.api.post("/api/admin/setup", {"name": "Dhinakaran C", "email": "dhina@proglint.com", "password": "Str0ng-Pass-9"}, format="json")
        self.assertEqual(r.status_code, 201)
        self.assertEqual(self.api.post("/api/admin/login", {"login": "dhina@proglint.com", "password": "Str0ng-Pass-9"}, format="json").status_code, 200)
