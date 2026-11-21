import json
import urllib.error
from datetime import timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import assistant, proposals
from .models import SoftwareItem, SoftwareRequest

PUBLIC = ["93.184.216.34"]
SHA = "c" * 64


def public_dns(host):
    return PUBLIC


class SafeUrlTests(SimpleTestCase):
    def check(self, url, resolves=PUBLIC):
        with mock.patch.object(proposals, "_resolve", return_value=resolves):
            try:
                return proposals.safe_public_url(url)
            except proposals.ProposalError:
                return None

    def test_only_public_https_addresses_are_accepted(self):
        self.assertEqual(self.check("https://www.notion.so/desktop"), "https://www.notion.so/desktop")
        for bad in ("http://notion.so/a", "ftp://notion.so/a", "https://user:pw@notion.so/a", "https://notion.so:8443/a", "https://notion.so:99999/a",
                    "https://127.0.0.1/a", "https://10.0.0.5/a", "https://[::1]/a", "https://localhost/a", "https://intranet/a", "https://wiki.corp/a",
                    "https://printer.local/a", "https://svc.internal/a", "javascript:alert(1)", "", None, "https://" + "a" * 1100 + ".com"):
            self.assertIsNone(self.check(bad), bad)

    def test_a_public_name_that_points_inside_the_network_is_refused(self):
        self.assertIsNone(self.check("https://evil.example/a", resolves=["10.1.2.3"]))
        self.assertIsNone(self.check("https://evil.example/a", resolves=["93.184.216.34", "192.168.1.9"]), "every address must be public")
        self.assertIsNone(self.check("https://evil.example/a", resolves=["169.254.169.254"]), "cloud metadata address")
        self.assertIsNone(self.check("https://evil.example/a", resolves=[]))
        with mock.patch.object(proposals, "_resolve", side_effect=OSError):
            self.assertRaises(proposals.ProposalError, proposals.safe_public_url, "https://nope.example/a")


class ProbeTests(SimpleTestCase):
    def response(self, status=206, headers=None, body=b""):
        res = mock.MagicMock()
        res.status, res.headers = status, headers or {}
        res.read.return_value = body
        res.__enter__.return_value = res
        return res

    def opener(self, *results):
        op = mock.MagicMock()
        op.open.side_effect = list(results)
        return mock.patch("urllib.request.build_opener", return_value=op)

    def redirect(self, to):
        return urllib.error.HTTPError("u", 302, "found", {"Location": to}, None)

    def test_it_reads_a_little_and_classifies(self):
        zipres = self.response(headers={"Content-Type": "application/zip", "Content-Range": "bytes 0-2047/53301345"}, body=b"PK\x03\x04" + b"\0" * 100)
        with mock.patch.object(proposals, "_resolve", return_value=PUBLIC), self.opener(zipres):
            r = proposals.probe("https://vendor.example/App.zip")
        self.assertEqual((r["ok"], r["looksLike"], r["size"], r["contentType"]), (True, "installer", 53301345, "application/zip"))
        page = self.response(status=200, headers={"Content-Type": "text/html; charset=utf-8"}, body=b"<!doctype html><html>")
        with mock.patch.object(proposals, "_resolve", return_value=PUBLIC), self.opener(page):
            self.assertEqual(proposals.probe("https://vendor.example/download")["looksLike"], "page")

    def test_every_redirect_hop_is_checked(self):
        hops = {"vendor.example": PUBLIC, "cdn.example": PUBLIC, "internal.example": ["10.0.0.9"]}
        ok = self.response(headers={"Content-Type": "application/x-apple-diskimage"}, body=b"x" * 10)
        with mock.patch.object(proposals, "_resolve", side_effect=lambda h: hops[h]), self.opener(self.redirect("https://cdn.example/f.dmg"), ok):
            r = proposals.probe("https://vendor.example/dl")
        self.assertEqual((r["ok"], r["finalUrl"]), (True, "https://cdn.example/f.dmg"))
        with mock.patch.object(proposals, "_resolve", side_effect=lambda h: hops[h]), self.opener(self.redirect("https://internal.example/secret")):
            r = proposals.probe("https://vendor.example/dl")
        self.assertFalse(r["ok"])
        self.assertIn("private network", r["reason"], "a redirect into the internal network is refused")
        with mock.patch.object(proposals, "_resolve", return_value=PUBLIC), self.opener(self.redirect("http://cdn.example/f")):
            self.assertFalse(proposals.probe("https://vendor.example/dl")["ok"], "a redirect down to plain http is refused")

    def test_failures_are_values_not_exceptions(self):
        with mock.patch.object(proposals, "_resolve", return_value=PUBLIC):
            with self.opener(urllib.error.HTTPError("u", 404, "nf", {}, None)):
                self.assertEqual(proposals.probe("https://vendor.example/x")["status"], 404)
            with self.opener(urllib.error.URLError("down")):
                self.assertFalse(proposals.probe("https://vendor.example/x")["ok"])
            with self.opener(*[self.redirect("https://vendor.example/loop")] * 6):
                self.assertEqual(proposals.probe("https://vendor.example/x")["reason"], "too many redirects")


class ModelAnswerTests(SimpleTestCase):
    GOOD = {"found": True, "name": "Notion", "vendor": "Notion Labs, Inc.", "homepage": "https://www.notion.so", "download_url": "https://www.notion.so/desktop",
            "license_kind": "freemium", "license_note": "Free for individuals; paid plans for teams.", "confidence": "high"}
    PAGE_OK = {"ok": True, "status": 200, "finalUrl": "https://www.notion.so/desktop", "contentType": "text/html", "size": None, "looksLike": "page"}

    def build(self, data, probe=None, dns=PUBLIC):
        with mock.patch.object(proposals, "_resolve", return_value=dns), mock.patch.object(proposals, "probe", return_value=probe or self.PAGE_OK):
            return proposals.build_proposal(data)

    def test_a_good_answer_becomes_a_proposal_with_pre_checks(self):
        p = self.build(self.GOOD)
        self.assertEqual((p["name"], p["vendor"], p["host"], p["licenseKind"], p["licenseClear"]), ("Notion", "Notion Labs, Inc.", "www.notion.so", "freemium", False))
        self.assertEqual(p["hostMatchesVendor"], "yes")
        self.assertTrue(all(c["ok"] for c in p["checks"]))
        self.assertEqual(p["source"], "ai")

    def test_nothing_trustworthy_means_no_proposal(self):
        for data in (None, {}, {"found": False}, {**self.GOOD, "name": " "}, {**self.GOOD, "download_url": "http://x.example/a", "homepage": ""},
                     {**self.GOOD, "download_url": "https://127.0.0.1/a", "homepage": "https://10.0.0.1/"}):
            self.assertIsNone(self.build(data), data)
        self.assertIsNone(self.build(self.GOOD, dns=["10.0.0.1"]), "every link points inside the network")

    def test_licence_and_text_are_normalised(self):
        p = self.build({**self.GOOD, "license_kind": "totally free forever!!", "name": "No<script>tion", "license_note": "x" * 900})
        self.assertEqual((p["licenseKind"], p["licenseLabel"]), ("unknown", "Licence not confirmed"), "unknown kinds are not taken at face value")
        self.assertEqual(p["name"], "Noscripttion")
        self.assertLessEqual(len(p["licenseNote"]), 240)
        self.assertTrue(self.build({**self.GOOD, "license_kind": "open_source"})["licenseClear"])
        self.assertFalse(self.build({**self.GOOD, "license_kind": "paid"})["licenseClear"])

    def test_problems_are_flagged_for_the_admin_not_hidden(self):
        dead = self.build(self.GOOD, probe={"ok": False, "status": 404, "reason": "the server answered 404"})
        self.assertFalse(dead["checks"][1]["ok"])
        self.assertIn("404", dead["checks"][1]["text"])
        odd = self.build({**self.GOOD, "download_url": "https://free-downloads.biz/notion.dmg", "homepage": ""},
                         probe={**self.PAGE_OK, "finalUrl": "https://free-downloads.biz/notion.dmg", "looksLike": "installer"})
        self.assertEqual(odd["hostMatchesVendor"], "no")
        self.assertFalse(odd["checks"][2]["ok"])
        self.assertIn("doesn't obviously belong", odd["checks"][2]["text"])

    def test_host_matching(self):
        self.assertEqual(proposals.host_matches("Notion", "Notion Labs", "https://www.notion.so/x"), "yes")
        self.assertEqual(proposals.host_matches("Slack", "Slack Technologies", "https://downloads.slack-edge.com/a"), "yes")
        self.assertEqual(proposals.host_matches("Rectangle", "Ryan Hanson", "https://github.com/rxhanson/Rectangle/releases"), "unknown")
        self.assertEqual(proposals.host_matches("Notion", "Notion Labs", "https://free-apps.biz/x"), "no")
        self.assertEqual(proposals.registrable_host("downloads.example.co.uk"), "example.co.uk")

    def test_the_models_json_is_parsed_tolerantly(self):
        self.assertEqual(proposals.parse_model_json('```json\n{"found": true}\n```'), {"found": True})
        self.assertEqual(proposals.parse_model_json('Sure! Here you go: {"found": false} Hope that helps.'), {"found": False})
        for bad in ("", "no json here", "{broken", "[1,2]", None):
            self.assertIsNone(proposals.parse_model_json(bad))


def item(slug, name, **kw):
    base = dict(version="1.0", platform="darwin", arch="any", installer_type="dmg", download_url=f"https://dl.example.com/{slug}.dmg",
                sha256="a" * 64, app_id=f"com.example.{slug}", app_name=f"{name}.app", requires_approval=True, enabled=True)
    base.update(kw)
    return SoftwareItem.objects.create(slug=slug, name=name, **base)


def search_says(**over):
    data = {**ModelAnswerTests.GOOD, **over}
    return mock.patch.object(assistant, "_call_gemini_search", return_value="```json\n" + json.dumps(data) + "\n```")


def wants_install():
    return mock.patch.object(assistant, "_call_gemini", return_value={"intent": "install", "reply": "x", "software": None})


@override_settings(GEMINI_API_KEY="test-key")
class NewSoftwareFlowTests(TestCase):
    def setUp(self):
        cache.clear()
        patcher = mock.patch.object(proposals, "_resolve", return_value=PUBLIC)
        patcher.start()
        self.addCleanup(patcher.stop)
        probe = mock.patch.object(proposals, "probe", return_value=ModelAnswerTests.PAGE_OK)
        probe.start()
        self.addCleanup(probe.stop)
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True, first_name="Asha", last_name="Admin")
        self.admin = APIClient()
        tok = self.admin.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.admin.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        self.agents = {}
        for aid, plat in (("PAP1", "darwin"), ("PAP2", "darwin")):
            code = self.admin.post("/api/admin/assets", {"assetId": aid, "employeeName": f"Emp {aid}", "employeeEmail": f"{aid}@x.co"}, format="json").json()["enrollCode"]
            c = APIClient()
            t = c.post("/api/agent/enroll", {"code": code, "platform": plat, "arch": "arm64"}, format="json").json()["token"]
            c.credentials(HTTP_AUTHORIZATION=f"Bearer {t}")
            self.agents[aid] = c
        self.agent = self.agents["PAP1"]
        item("slack", "Slack")

    def ask(self, text, key="k1", agent=None):
        return (agent or self.agent).post("/api/agent/software/requests", {"prompt": text, "idempotencyKey": key}, format="json")

    def propose(self, text="install notion", key="k1", **over):
        with wants_install(), search_says(**over):
            return self.ask(text, key).json()

    def good_item(self, **over):
        return {"name": "Notion", "version": "3.0.1", "publisher": "Notion Labs, Inc.", "platform": "darwin", "arch": "any", "installerType": "dmg",
                "downloadUrl": "https://desktop-release.notion-static.com/Notion-3.0.1.dmg", "sha256": SHA, "sizeBytes": 150_000_000, "appName": "Notion.app",
                "appId": "notion.id", "teamId": "LBQJ96FQ8D", "aliases": "notion app", "requiresApproval": True, **over}

    def approve(self, rid, version=None, item_data=None, licence=True):
        v = version if version is not None else SoftwareRequest.objects.get(pk=rid).request_version
        return self.admin.post(f"/api/admin/software/requests/{rid}/approve-new", {"expectedVersion": v, "licenceConfirmed": licence, "item": item_data or self.good_item()}, format="json")

    # ---- the employee's side

    def test_an_unapproved_app_is_looked_up_and_the_employee_is_asked_first(self):
        r = self.propose()
        self.assertEqual(r["status"], "PROPOSED")
        self.assertEqual(r["kind"], "new")
        self.assertIn("isn't on your approved software list yet", r["explanation"])
        self.assertIn("www.notion.so", r["explanation"])
        self.assertIn("free tier, paid plans for more", r["explanation"])
        self.assertEqual(r["proposal"], {"name": "Notion", "vendor": "Notion Labs, Inc.", "host": "www.notion.so", "licenseKind": "freemium", "licenseLabel": "Free tier, paid plans for more"})
        self.assertNotIn("downloadUrl", json.dumps(r), "employees never see download links")
        self.assertEqual(self.admin.get("/api/admin/software/approvals").json()["items"], [], "nothing reaches IT until the employee says yes")

    def test_yes_sends_it_to_the_admins_with_everything_they_need(self):
        rid = self.propose()["id"]
        c = self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json").json()
        self.assertEqual((c["status"], c["kind"]), ("AWAITING_APPROVAL", "new"))
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json").json()["status"], "AWAITING_APPROVAL", "twice is harmless")
        card = self.admin.get("/api/admin/software/approvals").json()["items"][0]
        self.assertEqual((card["kind"], card["employeeName"], card["assetPlatform"]), ("new", "Emp PAP1", "darwin"))
        p = card["proposal"]
        self.assertEqual((p["downloadUrl"], p["licenseKind"], p["licenseClear"], p["source"]), ("https://www.notion.so/desktop", "freemium", False, "ai"))
        self.assertTrue(p["checks"] and p["licenseNote"])
        kinds = [e["kind"] for e in self.admin.get(f"/api/admin/software/requests/{rid}").json()["events"]]
        self.assertEqual(kinds, ["requested", "proposed", "confirmed"])

    def test_no_drops_the_suggestion(self):
        rid = self.propose()["id"]
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid}/cancel", {}, format="json").json()["status"], "CANCELLED")
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json").json()["code"], "expired")

    def test_an_unconfirmed_suggestion_expires_quietly(self):
        rid = self.propose()["id"]
        SoftwareRequest.objects.filter(pk=rid).update(created_at=timezone.now() - timedelta(hours=2))
        self.agent.get("/api/agent/software/requests")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).decision_reason, "PROPOSAL_EXPIRED")
        self.assertEqual(self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json").json()["code"], "expired")

    def test_asking_again_shows_the_same_suggestion(self):
        a = self.propose(key="k1")
        b = self.propose("get me notion please", key="k2")
        self.assertEqual((a["id"], b["id"], b["replayed"]), (a["id"], a["id"], True))
        self.agent.post(f"/api/agent/software/requests/{a['id']}/confirm", {}, format="json")
        self.assertEqual(self.propose("install notion", key="k3")["id"], a["id"], "still with IT: no second request")
        self.assertEqual(SoftwareRequest.objects.filter(kind="new").count(), 1)

    def test_no_proposal_means_the_old_behaviour(self):
        with wants_install(), search_says(found=False):
            self.assertEqual(self.ask("install qwertyzzz").json()["status"], "NOT_FOUND")
        with wants_install(), mock.patch.object(assistant, "_call_gemini_search", return_value=None):
            self.assertEqual(self.ask("install qwertyyy", key="k2").json()["status"], "NOT_FOUND")
        with override_settings(GEMINI_API_KEY=""):
            self.assertEqual(self.ask("install notion", key="k3").json()["status"], "NOT_FOUND")

    @override_settings(PROPOSALS_PER_DAY=1)
    def test_lookups_are_limited_per_laptop(self):
        self.assertEqual(self.propose("install notion", key="k1")["status"], "PROPOSED")
        with wants_install(), search_says(name="Figma"):
            self.assertEqual(self.ask("install figma", key="k2").json()["status"], "NOT_FOUND", "over the allowance: no web lookup")

    def test_the_same_question_is_answered_from_cache(self):
        with wants_install(), search_says() as m:
            self.ask("install notion", "k1")
            self.ask("install notion", "k2", agent=self.agents["PAP2"])
        self.assertEqual(m.call_count, 1)

    def test_only_the_request_and_the_platform_reach_the_search(self):
        with mock.patch.object(assistant, "_call_gemini_search", return_value=None) as call:
            assistant.propose(SoftwareRequest.objects.model.asset.field.related_model.objects.get(asset_id="PAP1"), "install notion")
        system, prompt = call.call_args.args
        for secret in ("PAP1", "Emp PAP1", "PAP1@x.co"):
            self.assertNotIn(secret, system + prompt)
        self.assertIn("macOS", system)
        self.assertIn("<<<\ninstall notion\n>>>", prompt)

    # ---- the admin's side

    def pending(self):
        rid = self.propose()["id"]
        self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json")
        return rid

    def test_new_software_cannot_be_approved_without_checking_the_file(self):
        rid = self.pending()
        r = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": SoftwareRequest.objects.get(pk=rid).request_version}, format="json")
        self.assertEqual((r.status_code, r.json()["code"]), (409, "needs_review"))
        self.assertEqual(SoftwareItem.objects.filter(name="Notion").count(), 0)

    def test_approving_adds_it_to_the_catalogue_and_installs_it_then_it_is_available_to_everyone(self):
        rid = self.pending()
        r = self.approve(rid)
        self.assertEqual(r.status_code, 200, r.content)
        body = r.json()
        self.assertEqual((body["request"]["status"], body["request"]["decisionReason"]), ("QUEUED", "APPROVED_NEW"))
        self.assertEqual((body["item"]["slug"], body["item"]["sha256"], body["item"]["teamId"]), ("notion-darwin", SHA, "LBQJ96FQ8D"))
        job = self.agent.post("/api/agent/software/jobs/next", {}, format="json").json()["job"]
        self.assertEqual((job["requestId"], job["spec"]["sha256"], job["spec"]["name"]), (rid, SHA, "Notion"), "the laptop installs exactly the pinned, admin-checked file")
        events = [(e["kind"], e["actor"]) for e in self.admin.get(f"/api/admin/software/requests/{rid}").json()["events"]]
        self.assertIn(("approved_new", "admin:admin"), events)
        approved = [e for e in self.admin.get(f"/api/admin/software/requests/{rid}").json()["events"] if e["kind"] == "approved_new"][0]
        self.assertIn("confirmed by the admin", approved["detail"])
        self.assertIn("notion-darwin", approved["detail"])
        # available software for next time: another employee just asks, and it goes through the normal approval flow
        names = {i["name"] for i in self.agents["PAP2"].get("/api/agent/software/catalogue").json()["items"]}
        self.assertIn("Notion", names)
        r2 = self.ask("install notion", key="k9", agent=self.agents["PAP2"]).json()
        self.assertEqual((r2["status"], r2["kind"]), ("AWAITING_APPROVAL", "catalogue"))

    def test_software_that_is_not_clearly_free_needs_the_admin_to_confirm_the_licence(self):
        rid = self.pending()
        r = self.approve(rid, licence=False)
        self.assertEqual((r.status_code, r.json()["code"]), (409, "licence_unconfirmed"))
        self.assertEqual(SoftwareItem.objects.filter(name="Notion").count(), 0)
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).status, "AWAITING_APPROVAL")

    def test_clearly_free_software_needs_no_licence_confirmation(self):
        rid = self.propose("install rectangle", key="k5", name="Rectangle", vendor="Ryan Hanson", license_kind="open_source", download_url="https://rectangleapp.com/downloads")["id"]
        self.agent.post(f"/api/agent/software/requests/{rid}/confirm", {}, format="json")
        r = self.approve(rid, licence=False, item_data=self.good_item(name="Rectangle", appName="Rectangle.app"))
        self.assertEqual(r.status_code, 200, r.content)

    def test_a_wrong_build_changes_nothing(self):
        rid = self.pending()
        r = self.approve(rid, item_data=self.good_item(platform="win32", installerType="exe", silentArgs="/S"))
        self.assertEqual((r.status_code, r.json()["code"]), (400, "incompatible"))
        self.assertIn("Windows", r.json()["error"])
        self.assertEqual(SoftwareItem.objects.filter(name="Notion").count(), 0, "rolled back")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).status, "AWAITING_APPROVAL")

    def test_bad_input_and_races_are_reported_not_applied(self):
        rid = self.pending()
        bad = self.approve(rid, item_data=self.good_item(sha256="123"))
        self.assertEqual((bad.status_code, bad.json()["code"]), (400, "sha256"))
        self.assertEqual(self.approve(rid, version=99).json()["code"], "stale")
        self.assertEqual(self.admin.post(f"/api/admin/software/requests/{rid}/approve-new", {"expectedVersion": 2, "licenceConfirmed": True}, format="json").json()["code"], "item")
        item("notion-darwin", "Notion Old")  # the slug is taken
        self.assertEqual(self.approve(rid).json()["code"], "duplicate")
        self.assertEqual(SoftwareRequest.objects.get(pk=rid).status, "AWAITING_APPROVAL")

    def test_it_cannot_be_approved_twice_or_by_an_employee(self):
        rid = self.pending()
        self.assertEqual(self.approve(rid).status_code, 200)
        self.assertEqual(self.approve(rid).json()["code"], "not_pending")
        self.assertEqual(self.agent.post(f"/api/admin/software/requests/{rid}/approve-new", {"item": self.good_item()}, format="json").status_code, 401)

    def test_declining_new_software(self):
        rid = self.pending()
        v = SoftwareRequest.objects.get(pk=rid).request_version
        r = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "REJECTED", "expectedVersion": v, "comment": "Use Confluence"}, format="json").json()
        self.assertEqual(r["status"], "REJECTED")
        self.assertIn("Use Confluence", r["explanation"])

    def test_if_it_was_added_meanwhile_a_plain_approval_attaches_to_that_entry(self):
        rid = self.pending()
        item("notion", "Notion", platform="darwin")
        v = SoftwareRequest.objects.get(pk=rid).request_version
        r = self.admin.post(f"/api/admin/software/requests/{rid}/decision", {"decision": "APPROVED", "expectedVersion": v}, format="json").json()
        self.assertEqual((r["status"], r["softwareSlug"]), ("QUEUED", "notion"))

    def test_the_admin_history_shows_the_kind(self):
        rid = self.pending()
        row = [r for r in self.admin.get("/api/admin/software/requests").json()["items"] if r["id"] == rid][0]
        self.assertEqual(row["kind"], "new")
