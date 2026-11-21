from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import software as sw
from .models import Asset, InstalledApps, SoftwareItem, SoftwareRequest

SHA = "a" * 64


def item(slug, name, **kw):
    base = dict(version="1.0.0", platform="darwin", arch="any", installer_type="zip", download_url=f"https://dl.example.com/{slug}.zip",
                sha256=SHA, app_id=f"com.example.{slug}", app_name=f"{name}.app", requires_approval=True, enabled=True)
    base.update(kw)
    return SoftwareItem.objects.create(slug=slug, name=name, **base)


@override_settings(GEMINI_API_KEY="")  # tests never call the real model
class MatchingTests(TestCase):
    def setUp(self):
        self.asset = Asset.objects.create(asset_id="A1", employee_name="E", employee_email="e@x.co", platform="darwin", arch="arm64")
        self.vscode = item("vscode", "Visual Studio Code", aliases=["vscode", "vs code"], version="1.140.0")
        self.git = item("git", "Git", requires_approval=False)
        item("slack", "Slack")
        item("build-tools", "Visual Studio Build Tools", platform="win32", installer_type="exe")

    def ev(self, text):
        return sw.evaluate(self.asset, sw.clean_prompt(text))

    def test_natural_language_matching(self):
        for text, slug in [("Install git", "git"), ("can you please install Git for me?", "git"), ("I need vscode on my laptop", "vscode"),
                           ("install VS Code", "vscode"), ("slack", "slack"), ("Install the latest Visual Studio Code", "vscode")]:
            r = self.ev(text)
            self.assertEqual(r["item"].slug, slug, text)

    def test_unknown_gets_suggestion_not_a_guess(self):
        r = self.ev("install Slakc")
        self.assertEqual(r["status"], "NOT_FOUND")
        self.assertIn("Slack", r["explanation"])
        self.assertEqual(self.ev("install photoshop")["status"], "NOT_FOUND")

    def test_ambiguous_asks_which_one(self):
        item("vs-mac", "Visual Studio for Mac")
        r = self.ev("install visual studio")
        self.assertEqual(r["status"], "NEEDS_CLARIFICATION")
        self.assertGreaterEqual(len(r["candidates"]), 2)

    def test_empty_after_filler_needs_clarification(self):
        self.assertEqual(self.ev("please install the app")["status"], "NEEDS_CLARIFICATION")

    def test_other_platform_is_incompatible_not_missing(self):
        r = self.ev("install visual studio build tools")
        self.assertEqual(r["status"], "INCOMPATIBLE")
        self.assertIn("Windows", r["explanation"])

    def test_arch_specific_build_is_preferred(self):
        item("zoom-x64", "Zoom", arch="x64", requires_approval=False)
        arm = item("zoom-arm", "Zoom", arch="arm64", requires_approval=False)
        self.assertEqual(self.ev("zoom")["item"], arm)
        self.asset.arch = "x64"
        self.assertEqual(self.ev("zoom")["item"].slug, "zoom-x64")
        item("only-arm", "ArmOnly", arch="arm64")
        r = self.ev("armonly")  # an arm64-only build is never offered to an x64 laptop
        self.assertEqual((r["status"], r["reason"]), ("INCOMPATIBLE", "ARCH_MISMATCH"))

    def test_installed_versions(self):
        InstalledApps.objects.create(asset=self.asset, apps=[{"id": "com.example.git", "name": "Git", "version": "2.40.0"}])
        self.assertEqual(self.ev("install git")["reason"], "NEWER_VERSION_INSTALLED")
        InstalledApps.objects.filter(asset=self.asset).update(apps=[{"id": "com.example.git", "name": "Git", "version": "1.0.0"}])
        self.assertEqual(self.ev("install git")["reason"], "ALREADY_INSTALLED")
        InstalledApps.objects.filter(asset=self.asset).update(apps=[{"id": "com.example.git", "name": "Git", "version": "0.9"}])
        r = self.ev("install git")
        self.assertEqual(r["status"], "QUEUED")
        self.assertEqual(r["upgradeFrom"], "0.9")

    def test_an_old_installed_list_is_not_trusted(self):
        """The reported list is a snapshot: the app may have been uninstalled since (the bug: "already installed" right after uninstalling)."""
        row = InstalledApps.objects.create(asset=self.asset, apps=[{"id": "com.example.git", "name": "Git", "version": "1.0.0"}])
        self.assertEqual(self.ev("install git")["reason"], "ALREADY_INSTALLED", "a fresh report is believed")
        InstalledApps.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(hours=1))
        r = self.ev("install git")
        self.assertEqual(r["status"], "QUEUED", "an hour-old report is not")
        self.assertIsNone(r["upgradeFrom"])
        InstalledApps.objects.filter(pk=row.pk).update(updated_at=timezone.now() - timedelta(minutes=5))
        self.assertEqual(self.ev("install git")["status"], "ALREADY_INSTALLED", "five minutes is still fresh")

    def test_reinstall_skips_the_already_installed_shortcut_but_not_approval(self):
        InstalledApps.objects.create(asset=self.asset, apps=[{"id": "com.example.git", "name": "Git", "version": "1.0.0"},
                                                              {"id": "com.example.vscode", "name": "Visual Studio Code", "version": "1.140.0"}])
        installed = self.ev("install git")
        self.assertEqual((installed["status"], installed["candidates"]), ("ALREADY_INSTALLED", ["Git"]))
        self.assertIn("say “reinstall Git”", installed["explanation"], "the way out is offered")
        for text in ("reinstall git", "Re-install Git please", "install git again", "repair git"):
            self.assertEqual(self.ev(text)["status"], "QUEUED", text)
        self.assertEqual(sw.evaluate(self.asset, "install git", reinstall=True)["status"], "QUEUED", "the explicit flag works too")
        self.assertEqual(self.ev("reinstall visual studio code")["status"], "AWAITING_APPROVAL", "a reinstall still needs approval where the entry requires it")

    def test_input_rules(self):
        with self.assertRaises(sw.SoftwareError):
            sw.clean_prompt("   ")
        with self.assertRaises(sw.SoftwareError):
            sw.clean_prompt("x" * 501)
        with self.assertRaises(sw.SoftwareError):
            sw.clean_prompt("install git\x00")


@override_settings(GEMINI_API_KEY="")
class SoftwareFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True, first_name="Alice", last_name="Admin")
        User.objects.create_user("emp", password="s3cret-pass", is_staff=False)
        self.admin = APIClient()
        token = self.admin.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.admin.credentials(HTTP_AUTHORIZATION=f"Token {token}")
        self.agents = {}
        for aid in ("PAP1", "PAP2"):
            code = self.admin.post("/api/admin/assets", {"assetId": aid, "employeeName": aid, "employeeEmail": f"{aid}@x.co"}, format="json").json()["enrollCode"]
            c = APIClient()
            t = c.post("/api/agent/enroll", {"code": code, "hostname": aid, "platform": "darwin", "arch": "arm64"}, format="json").json()["token"]
            c.credentials(HTTP_AUTHORIZATION=f"Bearer {t}")
            self.agents[aid] = c
        self.agent = self.agents["PAP1"]
        self.vscode = item("vscode", "Visual Studio Code", aliases=["vscode"], version="1.140.0", size_bytes=1000)
        self.git = item("git", "Git", requires_approval=False)

    def ask(self, text, key="k1", agent=None):
        return (agent or self.agent).post("/api/agent/software/requests", {"prompt": text, "idempotencyKey": key}, format="json")

    def test_roles_are_separated(self):
        self.assertEqual(APIClient().get("/api/agent/software/catalogue").status_code, 401)
        self.assertEqual(self.agent.get("/api/admin/software/items").status_code, 401, "an employee laptop can't use admin endpoints")
        self.assertEqual(self.agent.post("/api/admin/software/requests/%s/decision" % "7f0c1a52-0000-4000-8000-000000000000", {}, format="json").status_code, 401)
        self.assertEqual(self.admin.get("/api/agent/software/catalogue").status_code, 401, "admin tokens aren't agent tokens")
        # a non-staff user token can't reach admin software APIs either
        emp = APIClient()
        r = emp.post("/api/admin/login", {"username": "emp", "password": "s3cret-pass"}, format="json")
        self.assertEqual(r.status_code, 403)

    def test_catalogue_only_lists_what_this_laptop_can_install(self):
        item("win-only", "WinApp", platform="win32", installer_type="exe")
        item("off", "Disabled", enabled=False)
        names = {i["name"] for i in self.agent.get("/api/agent/software/catalogue").json()["items"]}
        self.assertEqual(names, {"Visual Studio Code", "Git"})
        raw = self.agent.get("/api/agent/software/catalogue").content.decode()
        self.assertNotIn("dl.example.com", raw, "employees never see download links or digests")

    def test_preview_records_nothing(self):
        r = self.agent.post("/api/agent/software/preview", {"prompt": "install vscode"}, format="json").json()
        self.assertEqual(r["status"], "AWAITING_APPROVAL")
        self.assertEqual(SoftwareRequest.objects.count(), 0)

    def test_approval_flow_end_to_end(self):
        r = self.ask("Install VS Code")
        self.assertEqual(r.status_code, 201)
        req = r.json()
        self.assertEqual(req["status"], "AWAITING_APPROVAL")
        rid = req["id"]

        # same idempotency key / same software again -> the same request, not a duplicate
        again = self.ask("Install VS Code", key="k1")
        self.assertEqual((again.status_code, again.json()["id"], again.json()["replayed"]), (200, rid, True))
        twin = self.ask("please install vscode", key="k2")
        self.assertEqual((twin.json()["id"], twin.json()["replayed"]), (rid, True))
        self.assertEqual(SoftwareRequest.objects.count(), 1)

        # nothing to install until approved
        self.assertIsNone(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"])

        pending = self.admin.get("/api/admin/software/approvals").json()["items"]
        self.assertEqual([p["id"] for p in pending], [rid])
        self.assertEqual(pending[0]["employeeName"], "PAP1")
        self.assertEqual(pending[0]["sha256Prefix"], SHA[:12])
        self.assertEqual(self.admin.get("/api/admin/dashboard").json()["kpis"]["pendingApprovals"], 1)

        stale = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": 99}, format="json")
        self.assertEqual((stale.status_code, stale.json()["code"]), (409, "stale"))
        ok = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": req["requestVersion"], "comment": "ok"}, format="json")
        self.assertEqual(ok.json()["status"], "QUEUED")
        second = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "REJECTED", "expectedVersion": req["requestVersion"] + 1}, format="json")
        self.assertEqual(second.json()["code"], "not_pending")
        self.assertIn("Alice Admin", second.json()["error"])

        hb = self.agent.post("/api/agent/heartbeat", {"state": "tracking"}, format="json").json()
        self.assertEqual(hb["softwareJobs"], 1)
        job = self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"]
        self.assertEqual(job["requestId"], rid)
        self.assertEqual(job["spec"]["sha256"], SHA)
        self.assertIsNone(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"], "no second claim")
        self.assertIsNone(self.agents["PAP2"].post("/api/agent/software/jobs/next", {}, format="json").json()["job"], "other laptops never get it")
        other = self.agents["PAP2"].post(f"/api/agent/software/jobs/{rid}/result", {"ok": True}, format="json")
        self.assertEqual(other.status_code, 404, "a laptop can only report on its own request")

        for phase, status in (("DOWNLOADING", "DISPATCHED"), ("INSTALLING", "RUNNING"), ("VERIFYING", "VERIFYING"), ("DOWNLOADING", "VERIFYING")):
            p = self.agent.post(f"/api/agent/software/jobs/{rid}/progress", {"phase": phase, "bytesDone": 500, "bytesTotal": 1000}, format="json").json()
            self.assertEqual(p["status"], status, "progress never moves backwards")
        self.assertEqual(self.agent.post(f"/api/agent/software/jobs/{rid}/cancel", {}, format="json").status_code, 404)
        cancel = self.agent.post(f"/api/agent/software/requests/{rid}/cancel", {}, format="json")
        self.assertEqual(cancel.json()["code"], "already_running")

        done = self.agent.post(f"/api/agent/software/jobs/{rid}/result", {"ok": True}, format="json").json()
        self.assertEqual(done["status"], "SUCCEEDED")
        self.agent.post(f"/api/agent/software/jobs/{rid}/result", {"ok": False, "code": "DOWNLOAD_FAILED"}, format="json")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).status, "SUCCEEDED", "late duplicate reports are ignored")

        detail = self.admin.get(f"/api/admin/software/requests/{rid}").json()
        kinds = [e["kind"] for e in detail["events"]]
        self.assertEqual(kinds[0], "requested")
        for k in ("approved", "claimed", "succeeded"):
            self.assertIn(k, kinds)
        self.assertEqual(detail["decidedBy"], "Alice Admin")
        self.assertIn("admin:admin", [e["actor"] for e in detail["events"]])

    def test_auto_approved_goes_straight_to_the_queue(self):
        r = self.ask("install git").json()
        self.assertEqual(r["status"], "QUEUED")
        self.assertEqual(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"]["spec"]["slug"], "git")

    def test_reject_and_cancel(self):
        rid = self.ask("install vscode").json()["id"]
        v = SoftwareRequest.objects.get(pk=rid).request_version
        r = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "REJECTED", "expectedVersion": v, "comment": "Not licensed"}, format="json").json()
        self.assertEqual(r["status"], "REJECTED")
        self.assertIn("Not licensed", r["explanation"])
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid}/cancel", {}, format="json").json()["code"], "already_finished")
        rid2 = self.ask("install vscode", key="again").json()["id"]
        self.assertNotEqual(rid, rid2, "a rejected request doesn't block asking again")
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid2}/cancel", {}, format="json").json()["status"], "CANCELLED")
        self.assertEqual(self.admin.get("/api/admin/software/approvals").json()["items"], [])
        c = self.admin.post(f"/api/admin/software/requests/{rid2}/decision", {"decision": "APPROVED"}, format="json")
        self.assertEqual(c.json()["code"], "not_pending")

    def test_clarification_and_not_found(self):
        item("vs-mac", "Visual Studio for Mac")
        r = self.ask("install visual studio").json()
        self.assertEqual(r["status"], "NEEDS_CLARIFICATION")
        self.assertIsNone(r["id"])
        self.assertEqual(SoftwareRequest.objects.count(), 0, "questions aren't recorded as requests")
        nf = self.ask("install photoshop", key="k9").json()
        self.assertEqual(nf["status"], "NOT_FOUND")
        self.assertEqual(SoftwareRequest.objects.filter(status="NOT_FOUND").count(), 1, "missing software is recorded for admins")
        again = self.ask("Install Photoshop", key="k10").json()
        self.assertEqual((again["id"], again["replayed"]), (nf["id"], True), "asking again doesn't pile up rows")
        self.assertEqual(SoftwareRequest.objects.filter(status="NOT_FOUND").count(), 1)

    def test_nothing_to_do_answers_are_not_tracked(self):
        InstalledApps.objects.create(asset=Asset.objects.get(asset_id="PAP1"), apps=[{"id": "org.git", "name": "Git", "version": "9.9"}])
        r = self.ask("install git").json()
        self.assertEqual((r["status"], r["id"]), ("ALREADY_INSTALLED", None))
        self.assertIn("already", r["explanation"])
        item("winonly", "WinOnly", platform="win32", installer_type="exe")
        r = self.ask("install winonly", key="k2").json()
        self.assertEqual((r["status"], r["id"], r["decisionReason"]), ("INCOMPATIBLE", None, "PLATFORM_MISMATCH"))
        self.assertEqual(SoftwareRequest.objects.count(), 0)

    def test_bad_input_has_friendly_errors(self):
        self.assertEqual(self.ask("   ").json()["code"], "empty")
        self.assertEqual(self.ask("x" * 600).json()["code"], "too_long")
        r = self.agent.post("/api/agent/software/requests", {"prompt": "install git"}, format="json")
        self.assertEqual(r.json()["code"], "missing_key")

    @override_settings(SOFTWARE_MAX_OPEN_PER_ASSET=2)
    def test_too_many_open_requests(self):
        item("a", "Alpha")
        item("b", "Bravo")
        item("c", "Charlie")
        self.ask("alpha", "1")
        self.ask("bravo", "2")
        r = self.ask("charlie", "3")
        self.assertEqual((r.status_code, r.json()["code"]), (429, "too_many_open"))

    @override_settings(SOFTWARE_SUBMITS_PER_10MIN=2)
    def test_submit_rate_limit(self):
        self.ask("zzz1", "1")
        self.ask("zzz2", "2")
        self.assertEqual(self.ask("zzz3", "3").json()["code"], "rate_limited")

    def test_catalogue_change_after_request_blocks_the_install(self):
        rid = self.ask("install vscode").json()["id"]
        v = SoftwareRequest.objects.get(pk=rid).request_version
        self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": v}, format="json")
        self.admin.patch("/api/admin/software/items/vscode", {"sha256": "b" * 64}, format="json")
        self.assertIsNone(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"])
        req = SoftwareRequest.objects.get(pk=rid)
        self.assertEqual((req.status, req.decision_reason), ("BLOCKED", "CATALOGUE_CHANGED"))

    def test_approving_a_withdrawn_item_is_refused(self):
        rid = self.ask("install vscode").json()["id"]
        self.admin.patch("/api/admin/software/items/vscode", {"enabled": False}, format="json")
        v = SoftwareRequest.objects.get(pk=rid).request_version
        r = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": v}, format="json")
        self.assertEqual(r.json()["code"], "catalogue_changed")

    def test_stale_requests_are_closed(self):
        rid = self.ask("install git").json()["id"]
        SoftwareRequest.objects.filter(pk=rid).update(updated_at=timezone.now() - timedelta(hours=30))
        self.assertEqual(self.agent.get("/api/agent/software/requests").json()["items"][0]["status"], "EXPIRED")

        rid = self.ask("install vscode", "k5").json()["id"]
        SoftwareRequest.objects.filter(pk=rid).update(created_at=timezone.now() - timedelta(days=8))
        self.assertEqual(self.admin.get("/api/admin/software/approvals").json()["items"], [])
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).decision_reason, "APPROVAL_TIMEOUT")

    def test_interrupted_install_is_failed_and_a_late_success_recovers_it(self):
        rid = self.ask("install git").json()["id"]
        self.agent.post("/api/agent/software/jobs/next", {}, format="json")
        SoftwareRequest.objects.filter(pk=rid).update(progress_updated_at=timezone.now() - timedelta(minutes=30))
        self.agent.get("/api/agent/software/requests")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).decision_reason, "INSTALL_INTERRUPTED")
        p = self.agent.post(f"/api/agent/software/jobs/{rid}/progress", {"phase": "INSTALLING"}, format="json").json()
        self.assertFalse(p["continue"], "the agent is told to stop")
        self.agent.post(f"/api/agent/software/jobs/{rid}/result", {"ok": True}, format="json")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).status, "SUCCEEDED")

    def test_history_is_private_to_each_laptop(self):
        self.ask("install git")
        self.assertEqual(len(self.agent.get("/api/agent/software/requests").json()["items"]), 1)
        self.assertEqual(self.agents["PAP2"].get("/api/agent/software/requests").json()["items"], [])

    def test_installs_are_serialised_per_laptop(self):
        item("slack", "Slack", requires_approval=False)
        a = self.ask("install git", "1").json()["id"]
        self.ask("install slack", "2")
        first = self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"]["requestId"]
        self.assertEqual(first, a, "oldest first")
        self.assertIsNone(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"])
        self.agent.post(f"/api/agent/software/jobs/{a}/result", {"ok": False, "code": "DOWNLOAD_FAILED"}, format="json")
        self.assertIsNotNone(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"])

    def test_reinstall_over_the_api(self):
        InstalledApps.objects.create(asset=Asset.objects.get(asset_id="PAP1"), apps=[{"id": "com.example.git", "name": "Git", "version": "9.9"}])
        r = self.ask("install git").json()
        self.assertEqual((r["status"], r["id"], r["candidates"]), ("ALREADY_INSTALLED", None, ["Git"]))
        again = self.agent.post("/api/agent/software/requests", {"prompt": "install git", "idempotencyKey": "k-re", "reinstall": True}, format="json").json()
        self.assertEqual(again["status"], "QUEUED")
        self.assertEqual(self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"]["spec"]["slug"], "git")

    def test_a_simultaneous_duplicate_is_answered_with_the_first_request_not_an_error(self):
        first = self.ask("install git", "same-key").json()
        asset = Asset.objects.get(asset_id="PAP1")
        row = SoftwareRequest.objects.get(pk=first["id"])
        # The pre-check missed it (the other request was still in flight): the in-lock re-check finds it.
        with mock.patch.object(sw, "_find_existing", side_effect=[None, row]):
            again, replayed = sw.create_request(asset, "install git", "same-key")
        self.assertEqual((again.pk, replayed), (row.pk, True))
        # Even if both checks miss, the database constraint decides and the loser gets the winner back instead of a 500.
        with mock.patch.object(sw, "_find_existing", side_effect=[None, None, row]):
            again, replayed = sw.create_request(asset, "install git", "same-key")
        self.assertEqual((again.pk, replayed), (row.pk, True))
        self.assertEqual(SoftwareRequest.objects.count(), 1)

    def test_admin_catalogue_validation_and_crud(self):
        good = dict(name="Zoom", version="6.0", platform="darwin", installerType="dmg", downloadUrl="https://zoom.us/Zoom.dmg", sha256="C" * 64,
                    appId="us.zoom.xos", appName="zoom.us.app", teamId="BJ4HAAB9B3", aliases="zoom, zoom client", requiresApproval=False)
        r = self.admin.post("/api/admin/software/items", good, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        body = r.json()
        self.assertEqual((body["slug"], body["sha256"], body["aliases"]), ("zoom-darwin", "c" * 64, ["zoom", "zoom client"]))
        self.assertEqual(self.admin.post("/api/admin/software/items", good, format="json").status_code, 409)
        for patch, field in (({"downloadUrl": "http://evil.example/x.dmg"}, "downloadUrl"), ({"sha256": "123"}, "sha256"),
                             ({"installerType": "bat"}, "installerType"), ({"teamId": "bad"}, "teamId"), ({"appName": "../x.app"}, "appName"),
                             ({"platform": "amiga"}, "platform"), ({"silentArgs": ["a"] * 30}, "silentArgs")):
            r = self.admin.post("/api/admin/software/items", {**good, "name": "Other", **patch}, format="json")
            self.assertEqual((r.status_code, r.json().get("code")), (400, field), (patch, r.content))
        self.assertEqual(self.admin.patch("/api/admin/software/items/zoom-darwin", {"version": "6.1"}, format="json").json()["version"], "6.1")
        listing = self.admin.get("/api/admin/software/items").json()["items"]
        self.assertIn("zoom-darwin", [i["slug"] for i in listing])

        rid = self.ask("install vscode").json()["id"]
        busy = self.admin.delete("/api/admin/software/items/vscode")
        self.assertEqual((busy.status_code, busy.json()["code"]), (409, "in_use"))
        SoftwareRequest.objects.filter(pk=rid).update(status="CANCELLED")
        self.assertEqual(self.admin.delete("/api/admin/software/items/vscode").status_code, 204)
        kept = self.admin.get("/api/admin/software/requests?status=done").json()["items"]
        self.assertEqual(kept[0]["softwareName"], "Visual Studio Code", "history keeps the name after the item is removed")

    def test_unknown_uuid_is_404_not_500(self):
        r = self.agent.post("/api/agent/software/requests/7f0c1a52-0000-4000-8000-000000000000/cancel", {}, format="json")
        self.assertEqual(r.status_code, 404)
        self.assertEqual(self.agent.post("/api/agent/software/requests/not-a-uuid/cancel", {}, format="json").status_code, 404)

    def test_disconnecting_a_laptop_closes_its_open_requests(self):
        waiting = self.ask("install vscode").json()["id"]
        running = self.ask("install git", "k2").json()["id"]
        self.agent.post("/api/agent/software/jobs/next", {}, format="json")  # git is now being installed
        self.admin.post("/api/admin/assets/PAP1/reset-code")
        w, r = SoftwareRequest.objects.get(pk=waiting), SoftwareRequest.objects.get(pk=running)
        self.assertEqual((w.status, w.decision_reason), ("CANCELLED", "DEVICE_RESET"))
        self.assertEqual((r.status, r.decision_reason), ("FAILED", "INSTALL_INTERRUPTED"))
        self.assertEqual(self.admin.get("/api/admin/software/approvals").json()["items"], [], "nothing left to approve for a disconnected laptop")
        self.assertIn("disconnected", w.explanation)

    def test_app_icon_is_stored_listed_and_kept_private_from_employees(self):
        import base64
        png = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\nicon").decode()
        good = dict(name="Zoom", version="6.0", platform="darwin", installerType="dmg", downloadUrl="https://zoom.us/Zoom.dmg", sha256="c" * 64, icon=png)
        self.assertEqual(self.admin.post("/api/admin/software/items", good, format="json").json()["icon"], png)
        for bad in ("data:text/html;base64,PGgxPg==", "javascript:alert(1)", "data:image/png;base64," + "A" * (201 * 1024)):
            r = self.admin.post("/api/admin/software/items", {**good, "name": "Other", "icon": bad}, format="json")
            self.assertEqual((r.status_code, r.json()["code"]), (400, "icon"))
        self.assertEqual(self.admin.patch("/api/admin/software/items/vscode", {"version": "1.141.0"}, format="json").json()["icon"], None, "no icon is fine")
        rows = {i["slug"]: i for i in self.admin.get("/api/admin/software/items").json()["items"]}
        self.assertTrue(rows["zoom-darwin"]["hasIcon"])
        self.assertFalse(rows["vscode"]["hasIcon"])
        self.assertNotIn("icon", rows["zoom-darwin"], "lists stay light: icons are fetched separately")
        icons = self.admin.get("/api/admin/software/icons?slugs=zoom-darwin,vscode,nope").json()["icons"]
        self.assertEqual(icons, {"zoom-darwin": png}, "only entries that have an icon come back")
        self.assertEqual(self.agent.get("/api/admin/software/icons?slugs=zoom-darwin").status_code, 401, "admins only")
        self.assertNotIn("icon", self.agent.get("/api/agent/software/catalogue").json()["items"][0], "employees' catalogue stays minimal")
        rid = self.ask("install vscode").json()["id"]
        self.assertIsNone(self.admin.get("/api/admin/software/approvals").json()["items"][0]["icon"])


class LocalAdminTests(TestCase):
    """macOS .pkg installs ask for an administrator's password; admins are told when the employee isn't one."""

    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True)
        self.admin = APIClient()
        tok = self.admin.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.admin.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        self.code = self.admin.post("/api/admin/assets", {"assetId": "P1", "employeeName": "Priya", "employeeEmail": "p@x.co"}, format="json").json()["enrollCode"]

    def test_it_is_recorded_at_enrolment_and_kept_current(self):
        agent = APIClient()
        token = agent.post("/api/agent/enroll", {"code": self.code, "platform": "darwin", "arch": "arm64", "localAdmin": False}, format="json").json()["token"]
        agent.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        self.assertIs(Asset.objects.get(asset_id="P1").local_admin, False)
        agent.post("/api/agent/heartbeat", {"state": "tracking", "localAdmin": True}, format="json")
        self.assertIs(Asset.objects.get(asset_id="P1").local_admin, True)
        agent.post("/api/agent/heartbeat", {"state": "tracking"}, format="json")
        self.assertIs(Asset.objects.get(asset_id="P1").local_admin, True, "a heartbeat that doesn't say keeps what we knew")
        agent.post("/api/agent/heartbeat", {"state": "tracking", "localAdmin": "yes"}, format="json")
        self.assertIs(Asset.objects.get(asset_id="P1").local_admin, True, "anything that isn't a boolean is ignored")

    def test_admins_see_it_on_the_request_and_the_asset(self):
        agent = APIClient()
        token = agent.post("/api/agent/enroll", {"code": self.code, "platform": "darwin", "arch": "arm64", "localAdmin": False}, format="json").json()["token"]
        agent.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        item("zoomy", "Zoomy", installer_type="pkg", requires_approval=True)
        rid = agent.post("/api/agent/software/requests", {"prompt": "install zoomy", "idempotencyKey": "k1"}, format="json").json()["id"]
        card = self.admin.get("/api/admin/software/approvals").json()["items"][0]
        self.assertEqual((card["id"], card["installerType"], card["assetLocalAdmin"]), (rid, "pkg", False))
        self.assertIs(self.admin.get("/api/admin/assets/P1").json()["asset"]["localAdmin"], False)

    def test_an_agent_that_never_says_leaves_it_unknown(self):
        agent = APIClient()
        agent.post("/api/agent/enroll", {"code": self.code, "platform": "darwin"}, format="json")
        self.assertIsNone(Asset.objects.get(asset_id="P1").local_admin)


class PkgAppNameMatchTests(TestCase):
    def test_a_pkg_whose_app_is_named_differently_is_recognised_as_installed(self):
        from . import software
        from .models import Asset, InstalledApps, SoftwareItem
        asset = Asset.objects.create(hostname="h", platform="macos")
        item = SoftwareItem.objects.create(name="Zoom Workplace", app_id="us.zoom.pkg.videomeeting", app_name="zoom.us.app", installer_type="pkg",
                                           platform="macos", download_url="https://zoom.us/client/latest/Zoom.pkg", sha256="a" * 64, version="6.0")
        InstalledApps.objects.create(asset=asset, apps=[{"name": "zoom.us", "id": "us.zoom.xos", "version": "6.0.1"}])
        self.assertEqual(software.installed_version(asset, item), "6.0.1")
