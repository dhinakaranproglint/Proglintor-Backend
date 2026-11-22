"""Orbit: tasks, recognitions, briefing, chat. No real model or network is ever used."""
import json
from datetime import date, datetime, timedelta, timezone as dtz
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import orbit
from .models import Asset, DailyUsage, Recognition, Task

NOW = datetime(2026, 1, 14, 9, 15, tzinfo=dtz(timedelta(hours=5, minutes=30)))  # a Wednesday
TODAY = NOW.date()


class PureTests(SimpleTestCase):
    def test_salutation_boundaries(self):
        got = [orbit.salutation(h) for h in (4, 5, 11, 12, 16, 17, 20, 21)]
        self.assertEqual(got, ["Hello", "Good morning", "Good morning", "Good afternoon", "Good afternoon", "Good evening", "Good evening", "Hello"])

    def test_first_name(self):
        self.assertEqual(orbit.first_name("Asha Rao"), "Asha")
        self.assertEqual(orbit.first_name(""), "there")

    def test_local_time_needs_an_offset(self):
        for bad in (None, "", "garbage", "2026-01-12T09:15:00"):
            with self.assertRaises(orbit.OrbitError) as cm:
                orbit.parse_local_time(bad)
            self.assertEqual(cm.exception.status, 422)
        self.assertEqual(orbit.parse_local_time("2026-01-12T09:15:00+05:30").hour, 9)

    def test_agent_name_is_sanitised(self):
        self.assertEqual(orbit.clean_agent_name("<script>Nova</script>"), "scriptNovascript")
        self.assertEqual(orbit.clean_agent_name(""), "Orbit")
        self.assertLessEqual(len(orbit.clean_agent_name("x" * 100)), 24)

    def test_messages_are_validated_and_redacted(self):
        with self.assertRaises(orbit.OrbitError):
            orbit.clean_messages([])
        with self.assertRaises(orbit.OrbitError):
            orbit.clean_messages([{"role": "assistant", "content": "hi"}])
        with self.assertRaises(orbit.OrbitError):
            orbit.clean_messages([{"role": "user", "content": "x" * 1001}])
        out = orbit.clean_messages([{"role": "user", "content": "mail me at a.b@corp.com"}])
        self.assertNotIn("a.b@corp.com", out[0]["content"])


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class DataTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="asha@x.co")
        self.b = Asset.objects.create(asset_id="B1", employee_name="Bala K", employee_email="bala@x.co")

    def task(self, asset, title, due=None, status="todo", hours=4, percent=0, priority="medium", done_on=None):
        t = Task.objects.create(asset=asset, title=title, due_date=due, status=status, estimated_hours=hours, percent=percent, priority=priority)
        if done_on:
            Task.objects.filter(pk=t.pk).update(completed_at=done_on)
        return Task.objects.get(pk=t.pk)

    def test_tasks_are_bucketed_and_scoped(self):
        self.task(self.a, "Overdue one", TODAY - timedelta(days=2))
        self.task(self.a, "Due today", TODAY, priority="urgent")
        self.task(self.a, "Next week", TODAY + timedelta(days=3))
        self.task(self.b, "Bala secret", TODAY)
        s = orbit.task_sections(self.a, TODAY)
        self.assertEqual([t["title"] for t in s["overdue"]], ["Overdue one"])
        self.assertEqual([t["title"] for t in s["dueToday"]], ["Due today"])
        self.assertEqual([t["title"] for t in s["upcoming"]], ["Next week"])
        self.assertNotIn("Bala secret", json.dumps(s))

    def test_capacity_verdicts(self):
        self.assertEqual(orbit.capacity(self.a, TODAY)["verdict"], "SAFE")
        self.task(self.a, "Big", TODAY + timedelta(days=2), hours=22)   # 5 workdays x 6h = 30h available; 70% of that is 21h
        self.assertEqual(orbit.capacity(self.a, TODAY)["verdict"], "AT_RISK")
        self.task(self.a, "Bigger", TODAY + timedelta(days=2), hours=20)
        self.assertEqual(orbit.capacity(self.a, TODAY)["verdict"], "OVERLOAD")
        self.task(self.b, "Other person's", TODAY, hours=100)
        self.assertEqual(orbit.capacity(self.a, TODAY)["pendingHours"], 42.0)

    def test_progress_reduces_remaining_hours(self):
        t = self.task(self.a, "Half done", TODAY, hours=10, percent=50)
        self.assertEqual(orbit.remaining_hours(t), 5.0)

    def test_tools_are_closed_over_one_employee(self):
        self.task(self.b, "Bala secret", TODAY)
        decls, impl = orbit.employee_tools(self.a, TODAY)
        names = {d["name"] for d in decls}
        self.assertEqual(names, {"get_my_tasks", "get_my_usage", "get_my_achievements", "get_my_recognitions", "get_my_meetings",
                                 "get_my_code_activity", "get_my_workload", "get_my_timesheet", "tell_reporting_person_no_tasks"})
        self.assertNotIn("employee", json.dumps(decls).lower().replace("the employee's", ""))
        for d in decls:
            for prop in (d.get("parameters") or {}).get("properties", {}):
                self.assertNotIn("id", prop.lower().replace("days", ""))
            if "parameters" in d:
                self.assertTrue(d["parameters"]["properties"], "a tool with no arguments must omit `parameters`")
        for name, fn in impl.items():
            self.assertNotIn("Bala secret", json.dumps(fn({"days": 1000}), default=str))
            self.assertNotIn("Bala secret", json.dumps(fn({"days": -5}), default=str))

    def test_usage_summary_and_streak(self):
        for i in range(1, 6):
            d = TODAY - timedelta(days=i)
            DailyUsage.objects.create(asset=self.a, day=d, app_id="code", app_name="VS Code", seconds=5 * 3600)
        DailyUsage.objects.create(asset=self.b, day=TODAY - timedelta(days=1), app_id="x", app_name="Secret", seconds=9999)
        u = orbit.usage_summary(self.a, TODAY)
        self.assertEqual(u["yesterdaySeconds"], 5 * 3600)
        self.assertEqual(u["topAppsYesterday"][0]["app"], "VS Code")
        self.assertNotIn("Secret", json.dumps(u))
        steady = next(m for m in orbit.achievements(self.a, TODAY) if m["id"] == "steady")
        self.assertGreaterEqual(steady["value"], 3)
        self.assertTrue(steady["earned"])

    def test_candidates_and_nomination_flow(self):
        for i in range(3):
            self.task(self.a, f"Done {i}", TODAY + timedelta(days=5), status="done", percent=100, done_on=NOW - timedelta(days=1))
        found = orbit.candidates(self.a, TODAY)
        kinds = {c["kind"] for c in found}
        self.assertIn("delivery", kinds)
        self.assertIn("early", kinds)
        key = next(c["dedupeKey"] for c in found if c["kind"] == "delivery")
        rec = orbit.nominate(self.a, TODAY, key)
        self.assertEqual((rec.status, rec.source), ("pending", "orbit"))
        self.assertNotIn(key, {c["dedupeKey"] for c in orbit.candidates(self.a, TODAY)}, "sent once, never offered again")
        with self.assertRaises(orbit.OrbitError) as cm:
            orbit.nominate(self.a, TODAY, key)
        self.assertEqual(cm.exception.status, 409)
        with self.assertRaises(orbit.OrbitError):
            orbit.nominate(self.b, TODAY, key)  # someone else's achievement can't be claimed

    def test_self_nomination_limits(self):
        with self.assertRaises(orbit.OrbitError):
            orbit.nominate(self.a, TODAY, None, "x", "")
        for i in range(3):
            orbit.nominate(self.a, TODAY, None, f"Covered the incident {i}", "details")
        with self.assertRaises(orbit.OrbitError) as cm:
            orbit.nominate(self.a, TODAY, None, "One more thing", "")
        self.assertEqual(cm.exception.status, 429)

    def test_decision_awards_credits_and_certificate_once(self):
        admin = User.objects.create_user("boss", is_staff=True)
        rec = orbit.nominate(self.a, TODAY, None, "Covered the production incident", "Saturday, 5 hours")
        self.assertEqual(orbit.credits_total(self.a), 0, "nothing is awarded before an admin decides")
        orbit.decide(rec, admin, "approve", "Comp-off day", 1.0, "Thank you!")
        self.assertEqual(orbit.credits_total(self.a), 1.0)
        self.assertTrue(rec.certificate_id.startswith("CERT-"))
        with self.assertRaises(orbit.OrbitError) as cm:
            orbit.decide(rec, admin, "reject")
        self.assertEqual(cm.exception.status, 409)
        r2 = orbit.nominate(self.a, TODAY, None, "Another extra", "")
        orbit.decide(r2, admin, "reject", "", 5, "Not this time")
        self.assertEqual(orbit.credits_total(self.a), 1.0, "a rejection awards nothing")

    def test_certificate_escapes_everything(self):
        admin = User.objects.create_user("boss2", is_staff=True)
        self.a.employee_name = "<img src=x onerror=alert(1)>"
        self.a.save()
        rec = orbit.award_direct(self.a, admin, "<script>alert(1)</script>", "\"><b>x</b>", "<i>gold</i>", 1)
        out = orbit.certificate_html(rec)
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img src=x", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("default-src 'none'", out)

    def test_template_briefing_without_a_model(self):
        self.task(self.a, "Ship the report", TODAY - timedelta(days=1), priority="high")
        b = orbit.briefing(self.a, NOW)
        self.assertEqual(b["greeting"]["text"], "Good morning, Asha!")
        self.assertEqual(b["messageSource"], "template")
        self.assertIn("Ship the report", b["message"])
        self.assertEqual(b["ask"], "How can I help you today?")
        self.assertEqual(b["workData"], "off")
        self.assertNotIn("meetings", b["sections"], "no source configured: nothing is invented")

    def test_rule_replies_use_real_data_only(self):
        self.assertIn("don't see any open tasks", orbit.chat(self.a, [{"role": "user", "content": "what should I focus on first?"}], NOW)["reply"])
        self.task(self.a, "Fix login", TODAY, priority="urgent")
        r = orbit.chat(self.a, [{"role": "user", "content": "what should I focus on first?"}], NOW)
        self.assertIn("Fix login", r["reply"])
        self.assertEqual(r["source"], "rules")

    def test_install_requests_are_routed_not_answered(self):
        r = orbit.chat(self.a, [{"role": "user", "content": "please install Slack"}], NOW)
        self.assertEqual(r["actions"], [{"type": "goto", "page": "install"}])


class FakeModel:
    """Turn 1 asks for a tool; turn 2 answers using what the tool returned."""
    def __init__(self, tool="get_my_tasks"):
        self.tool, self.bodies = tool, []

    def __call__(self, body, timeout):
        self.bodies.append(json.loads(json.dumps(body)))
        last = body["contents"][-1]
        if last["role"] == "user" and "functionResponse" in last["parts"][0]:
            return {"candidates": [{"content": {"parts": [{"text": "Result: " + json.dumps(last["parts"][0]["functionResponse"]["response"])[:300]}]}}]}
        return {"candidates": [{"content": {"parts": [{"functionCall": {"name": self.tool, "args": {}}, "thoughtSignature": "sig"}]}}]}


@override_settings(GEMINI_API_KEY="fake", ASSISTANT_CHAT_ENABLED=True, ORBIT_WORK_SOURCE="off")
class ModelLoopTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="{Rao} Asha", employee_email="asha@x.co")
        self.b = Asset.objects.create(asset_id="B1", employee_name="Bala", employee_email="bala@x.co")
        Task.objects.create(asset=self.a, title="Fix login", due_date=TODAY, priority="urgent")
        Task.objects.create(asset=self.b, title="Bala secret", due_date=TODAY)

    def test_tool_result_reaches_the_model_and_the_reply(self):
        fake = FakeModel()
        with mock.patch("pulse.assistant._post", fake):
            r = orbit.chat(self.a, [{"role": "user", "content": "what is due?"}], NOW, "Nova")
        self.assertEqual(r["source"], "llm")
        self.assertIn("Fix login", r["reply"])
        self.assertNotIn("Bala secret", json.dumps(fake.bodies))
        second = fake.bodies[1]
        self.assertEqual(second["contents"][1]["parts"][0]["thoughtSignature"], "sig", "the model's parts are echoed back unchanged")
        self.assertIn("Nova", second["systemInstruction"]["parts"][0]["text"])
        self.assertIn("{Rao}", second["systemInstruction"]["parts"][0]["text"], "braces in a name must not break the prompt")

    def test_unknown_tool_is_an_error_result_not_a_crash(self):
        fake = FakeModel(tool="get_attendance_of_everyone")
        with mock.patch("pulse.assistant._post", fake):
            r = orbit.chat(self.a, [{"role": "user", "content": "hello"}], NOW)
        self.assertIn("unknown tool", r["reply"])

    def test_endless_tool_calls_stop(self):
        always = lambda body, timeout: {"candidates": [{"content": {"parts": [{"functionCall": {"name": "get_my_tasks", "args": {}}}]}}]}  # noqa: E731
        with mock.patch("pulse.assistant._post", always):
            r = orbit.chat(self.a, [{"role": "user", "content": "hello"}], NOW)
        self.assertIn("narrow", r["reply"])

    def test_model_failure_falls_back_to_rules(self):
        with mock.patch("pulse.assistant._post", return_value=None):
            r = orbit.chat(self.a, [{"role": "user", "content": "what should I focus on?"}], NOW)
        self.assertEqual(r["source"], "rules")
        self.assertIn("Fix login", r["reply"])

    def test_briefing_message_from_the_model_is_cached(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "Start with Fix login today."}]}}]}
        with mock.patch("pulse.assistant._post", return_value=reply) as post:
            b1 = orbit.briefing(self.a, NOW)
            b2 = orbit.briefing(self.a, NOW)
        self.assertEqual((b1["messageSource"], b1["message"]), ("llm", "Start with Fix login today."))
        self.assertEqual(post.call_count, 1, "the same day's data is narrated once")
        sent = json.dumps(post.call_args[0][0])
        self.assertNotIn("Bala", sent)
        self.assertNotIn("asha@x.co", sent)
        self.assertEqual(b2["messageSource"], "llm")


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class ApiTests(TestCase):
    def setUp(self):
        cache.clear()
        User.objects.create_user("admin", password="s3cret-pass", is_staff=True)
        self.admin = APIClient()
        tok = self.admin.post("/api/admin/login", {"username": "admin", "password": "s3cret-pass"}, format="json").json()["token"]
        self.admin.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        self.agents = {}
        for aid in ("PAP1", "PAP2"):
            code = self.admin.post("/api/admin/assets", {"assetId": aid, "employeeName": f"Emp {aid}", "employeeEmail": f"{aid}@x.co"}, format="json").json()["enrollCode"]
            c = APIClient()
            t = c.post("/api/agent/enroll", {"code": code, "hostname": aid, "platform": "darwin"}, format="json").json()["token"]
            c.credentials(HTTP_AUTHORIZATION=f"Bearer {t}")
            self.agents[aid] = c
        self.me, self.other = self.agents["PAP1"], self.agents["PAP2"]
        self.lt = "2026-01-14T09:15:00+05:30"

    def assign(self, **kw):
        body = {"title": "Migrate the cache", "priority": "high", "estimatedHours": 8, "dueDate": "2026-01-20", "assetIds": ["PAP1"], **kw}
        return self.admin.post("/api/admin/orbit/tasks", body, format="json")

    def test_roles_are_separated(self):
        self.assertEqual(APIClient().get("/api/agent/orbit/briefing", {"localTime": self.lt}).status_code, 401)
        self.assertEqual(self.admin.get("/api/agent/orbit/tasks").status_code, 401, "an admin token isn't an agent token")
        self.assertEqual(self.me.get("/api/admin/orbit/tasks").status_code, 401, "an agent token can't reach admin APIs")
        self.assertEqual(self.me.post("/api/admin/orbit/chat", {"messages": []}, format="json").status_code, 401)

    def test_briefing_validates_time_and_greets(self):
        self.assertEqual(self.me.get("/api/agent/orbit/briefing").status_code, 422)
        self.assertEqual(self.me.get("/api/agent/orbit/briefing", {"localTime": "2026-01-14T09:15:00"}).status_code, 422)
        r = self.me.get("/api/agent/orbit/briefing", {"localTime": self.lt}).json()
        self.assertEqual(r["greeting"]["text"], "Good morning, Emp!")
        evening = self.me.get("/api/agent/orbit/briefing", {"localTime": "2026-01-14T19:30:00+05:30"}).json()
        self.assertEqual(evening["greeting"]["salutation"], "Good evening")

    def test_assigning_tasks_to_several_people_with_feasibility(self):
        r = self.assign(assetIds=["PAP1", "PAP2"], estimatedHours=40, dueDate=str(timezone.localdate() + timedelta(days=2)))
        self.assertEqual(r.status_code, 201)
        self.assertEqual(len(r.json()["items"]), 2)
        self.assertEqual({f["verdict"] for f in r.json()["feasibility"]}, {"OVERLOAD"})
        self.assertEqual(len(self.me.get("/api/agent/orbit/tasks", {"localTime": self.lt}).json()["later"]) + len(self.me.get("/api/agent/orbit/tasks", {"localTime": self.lt}).json()["upcoming"]), 1)

    def test_task_validation(self):
        self.assertEqual(self.assign(title="x").status_code, 422)
        self.assertEqual(self.assign(priority="nope").status_code, 422)
        self.assertEqual(self.assign(estimatedHours=-1).status_code, 422)
        self.assertEqual(self.assign(dueDate="soon").status_code, 422)
        self.assertEqual(self.assign(assetIds=[]).status_code, 422)
        self.assertEqual(self.assign(assetIds=["NOPE"]).status_code, 422)

    def test_employee_updates_only_their_own_task(self):
        tid = self.assign().json()["items"][0]["id"]
        self.assertEqual(self.other.patch(f"/api/agent/orbit/tasks/{tid}", {"status": "done"}, format="json").status_code, 404)
        r = self.me.patch(f"/api/agent/orbit/tasks/{tid}", {"status": "in_progress", "percent": 40}, format="json")
        self.assertEqual((r.status_code, r.json()["percent"]), (200, 40))
        self.assertEqual(self.me.patch(f"/api/agent/orbit/tasks/{tid}", {"percent": 101}, format="json").status_code, 422)
        self.assertEqual(self.me.patch(f"/api/agent/orbit/tasks/{tid}", {"status": "weird"}, format="json").status_code, 422)
        done = self.me.patch(f"/api/agent/orbit/tasks/{tid}", {"status": "done"}, format="json").json()
        self.assertEqual((done["status"], done["percent"]), ("done", 100))
        self.assertIsNotNone(done["completedAt"])

    def test_recognition_end_to_end(self):
        asset = Asset.objects.get(asset_id="PAP1")
        for i in range(3):
            t = Task.objects.create(asset=asset, title=f"T{i}", status="done", percent=100, due_date=timezone.localdate() + timedelta(days=9))
            Task.objects.filter(pk=t.pk).update(completed_at=timezone.now() - timedelta(hours=3))
        a = self.me.get("/api/agent/orbit/achievements", {"localTime": self.lt}).json()
        self.assertTrue(a["suggestions"])
        key = a["suggestions"][0]["dedupeKey"]
        sent = self.me.post("/api/agent/orbit/recognitions", {"dedupeKey": key, "localTime": self.lt}, format="json")
        self.assertEqual(sent.status_code, 201)
        self.assertEqual(self.me.post("/api/agent/orbit/recognitions", {"dedupeKey": key, "localTime": self.lt}, format="json").status_code, 409)
        rid = sent.json()["id"]
        self.assertEqual(self.me.get(f"/api/agent/orbit/recognitions/{rid}/certificate").status_code, 404, "no certificate before approval")
        pending = self.admin.get("/api/admin/orbit/recognitions", {"status": "pending"}).json()
        self.assertEqual(pending["pending"], 1)
        self.assertEqual(pending["items"][0]["employeeName"], "Emp PAP1")
        self.assertEqual(self.admin.post(f"/api/admin/orbit/recognitions/{rid}/decision", {"decision": "maybe"}, format="json").status_code, 422)
        ok = self.admin.post(f"/api/admin/orbit/recognitions/{rid}/decision", {"decision": "approve", "reward": "Extra day off", "credits": 1, "note": "Well done"}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(self.admin.post(f"/api/admin/orbit/recognitions/{rid}/decision", {"decision": "approve"}, format="json").status_code, 409)
        cert = self.me.get(f"/api/agent/orbit/recognitions/{rid}/certificate").json()
        self.assertIn("Extra day off", cert["html"])
        self.assertEqual(self.other.get(f"/api/agent/orbit/recognitions/{rid}/certificate").status_code, 404, "someone else's certificate")
        self.assertEqual(self.me.get("/api/agent/orbit/achievements", {"localTime": self.lt}).json()["credits"], 1.0)
        pulse = self.me.get("/api/agent/orbit/pulse").json()
        self.assertEqual(pulse["recognitions"][0]["status"], "approved")

    def test_admin_can_recognise_directly_and_see_the_team(self):
        r = self.admin.post("/api/admin/orbit/recognitions", {"assetId": "PAP2", "title": "Saved the release", "reward": "Gift card", "credits": 2}, format="json")
        self.assertEqual((r.status_code, r.json()["status"]), (201, "approved"))
        team = {x["assetId"]: x for x in self.admin.get("/api/admin/orbit/team").json()["items"]}
        self.assertEqual(team["PAP2"]["credits"], 2.0)

    def test_chat_validation_and_fallback(self):
        self.assertEqual(self.me.post("/api/agent/orbit/chat", {"messages": []}, format="json").status_code, 422)
        r = self.me.post("/api/agent/orbit/chat", {"messages": [{"role": "user", "content": "any tasks?"}], "localTime": self.lt}, format="json")
        self.assertEqual((r.status_code, r.json()["source"]), (200, "rules"))
        self.assertEqual(self.admin.post("/api/admin/orbit/chat", {"messages": [{"role": "user", "content": "hi"}]}, format="json").status_code, 503)

    @override_settings(ORBIT_CHAT_PER_10MIN=2)
    def test_chat_is_rate_limited(self):
        body = {"messages": [{"role": "user", "content": "hi"}], "localTime": self.lt}
        codes = [self.me.post("/api/agent/orbit/chat", body, format="json").status_code for _ in range(3)]
        self.assertEqual(codes, [200, 200, 429])

    @override_settings(ORBIT_ENABLED=False)
    def test_can_be_switched_off(self):
        self.assertEqual(self.me.get("/api/agent/orbit/briefing", {"localTime": self.lt}).status_code, 404)

    def test_admin_tools_are_read_only_and_cover_the_team(self):
        decls, impl = orbit.admin_tools(TODAY)
        self.assertEqual({d["name"] for d in decls}, {"list_employees", "get_team_workload", "get_tasks", "list_recognitions", "get_credit_leaderboard", "get_usage_summary"})
        before = (Task.objects.count(), Recognition.objects.count())
        for fn in impl.values():
            fn({"assetId": "PAP1", "days": 5, "status": "done"})
        self.assertEqual(before, (Task.objects.count(), Recognition.objects.count()))


class WorkSourceTests(SimpleTestCase):
    def test_mode_selection(self):
        from . import worksource
        with override_settings(ORBIT_WORK_SOURCE="auto", DEBUG=False, ADO_ORG_URL="", ADO_PAT="", AZURE_CLIENT_ID=""):
            self.assertEqual(worksource.mode(), "off")
        with override_settings(ORBIT_WORK_SOURCE="auto", DEBUG=True, ADO_ORG_URL="", ADO_PAT="", AZURE_CLIENT_ID=""):
            self.assertEqual(worksource.mode(), "simulated")
        with override_settings(ORBIT_WORK_SOURCE="auto", DEBUG=False, ADO_ORG_URL="https://dev.azure.com/o", ADO_PAT="p", ADO_PROJECT="x"):
            self.assertEqual(worksource.mode(), "live")

    def test_simulated_data_is_deterministic_and_plausible(self):
        from . import worksource
        a = Asset(asset_id="A1")
        with override_settings(ORBIT_WORK_SOURCE="simulated"):
            m1 = worksource.meetings(a, date(2026, 1, 14), date(2026, 1, 14))
            m2 = worksource.meetings(a, date(2026, 1, 14), date(2026, 1, 14))
            self.assertEqual(m1, m2)
            self.assertTrue(2 <= len(m1) <= 4)
            self.assertEqual(worksource.meetings(a, date(2026, 1, 17), date(2026, 1, 17)), [], "no meetings on a Saturday")

    def test_a_failing_live_source_becomes_an_empty_section(self):
        from . import worksource
        with override_settings(ORBIT_WORK_SOURCE="live", AZURE_TENANT_ID="t", AZURE_CLIENT_ID="c", AZURE_CLIENT_SECRET="s"), \
                mock.patch.object(worksource, "_graph_token", side_effect=OSError("down")):
            self.assertEqual(worksource.meetings(Asset(asset_id="A1", employee_email="a@x.co"), date(2026, 1, 14), date(2026, 1, 14)), [])


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class WrapupTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="asha@x.co")
        self.b = Asset.objects.create(asset_id="B1", employee_name="Bala", employee_email="bala@x.co")

    def test_wrapup_counts_only_todays_real_work(self):
        t = Task.objects.create(asset=self.a, title="Ship the report", status="done", percent=100, estimated_hours=2)
        Task.objects.filter(pk=t.pk).update(completed_at=NOW - timedelta(hours=1))
        old = Task.objects.create(asset=self.a, title="Old win", status="done", percent=100)
        Task.objects.filter(pk=old.pk).update(completed_at=NOW - timedelta(days=3))
        Task.objects.create(asset=self.a, title="Write the docs", due_date=TODAY + timedelta(days=1), priority="high")
        Task.objects.create(asset=self.b, title="Bala only", status="done", percent=100)
        DailyUsage.objects.create(asset=self.a, day=TODAY, app_id="code", app_name="VS Code", seconds=5.5 * 3600)
        DailyUsage.objects.create(asset=self.b, day=TODAY, app_id="x", app_name="Secret", seconds=9999)
        w = orbit.wrapup(self.a, NOW)
        self.assertEqual(w["doneToday"], ["Ship the report"])
        self.assertEqual(w["nextUp"][0]["title"], "Write the docs")
        self.assertEqual(w["activeSeconds"], 5.5 * 3600)
        self.assertNotIn("Bala", json.dumps(w))
        self.assertIn("5.5 hours", w["message"])
        self.assertIn("Write the docs", w["message"])
        self.assertEqual(w["messageSource"], "template")

    def test_a_day_with_no_activity_says_so(self):
        self.assertIn("don't see any activity", orbit.wrapup(self.a, NOW)["message"])

    def test_wrapup_endpoint_needs_a_local_time_and_a_laptop(self):
        c = APIClient()
        self.assertEqual(c.get("/api/agent/orbit/wrapup", {"localTime": "2026-01-14T18:00:00+05:30"}).status_code, 401)


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="simulated")
class RealUsageTests(TestCase):
    """Only meetings and tasks may be demo data. Everything about app time comes from what the laptop really reported."""

    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="asha@x.co")
        self.b = Asset.objects.create(asset_id="B1", employee_name="Bala", employee_email="bala@x.co")
        DailyUsage.objects.create(asset=self.a, day=TODAY, app_id="com.microsoft.VSCode", app_name="Visual Studio Code", seconds=2.5 * 3600 + 30 * 60)
        DailyUsage.objects.create(asset=self.a, day=TODAY, app_id="com.google.Chrome", app_name="Google Chrome", seconds=1.25 * 3600)
        DailyUsage.objects.create(asset=self.a, day=TODAY - timedelta(days=1), app_id="com.microsoft.VSCode", app_name="Visual Studio Code", seconds=4 * 3600)
        DailyUsage.objects.create(asset=self.b, day=TODAY, app_id="com.microsoft.VSCode", app_name="Visual Studio Code", seconds=9 * 3600)

    def test_hours_today_in_one_app_are_the_real_numbers(self):
        for typed in ("VS Code", "vscode", "Visual Studio Code", "code"):
            d = orbit.usage_detail(self.a, TODAY, 1, typed)
            self.assertEqual((d["found"], d["today"]["hours"], d["today"]["minutes"]), (True, 3.0, 180), typed)
        self.assertEqual(orbit.usage_detail(self.a, TODAY, 1, "chrome")["today"]["minutes"], 75)
        self.assertNotIn("540", json.dumps(orbit.usage_detail(self.a, TODAY, 3, "vscode")), "another person's 9 hours never appears")

    def test_an_app_with_no_time_says_so_instead_of_guessing(self):
        d = orbit.usage_detail(self.a, TODAY, 1, "Photoshop")
        self.assertFalse(d["found"])
        self.assertEqual(d["today"]["hours"], 0)
        self.assertIn("No time recorded", d["note"])

    def test_earlier_days_and_totals(self):
        d = orbit.usage_detail(self.a, TODAY, 2, "vscode")
        self.assertEqual(d["perDayHours"][(TODAY - timedelta(days=1)).isoformat()], 4.0)
        self.assertEqual(orbit.usage_detail(self.a, TODAY, 1)["allAppsTodayHours"], 4.2)  # 3.0h + 1.25h, rounded

    def test_the_tool_and_the_no_model_answer_use_the_same_real_data(self):
        _, impl = orbit.employee_tools(self.a, TODAY)
        self.assertEqual(impl["get_my_usage"]({"app": "VS Code"})["today"]["minutes"], 180)
        r = orbit.chat(self.a, [{"role": "user", "content": "how many hours today did I spend on VS Code?"}], NOW)
        self.assertEqual(r["source"], "rules")
        self.assertIn("3 hours", r["reply"])
        self.assertIn("Visual Studio Code", r["reply"])
        none = orbit.chat(self.a, [{"role": "user", "content": "how many hours today on Photoshop"}], NOW)["reply"]
        self.assertIn("don't see any time recorded in photoshop", none.lower())
        y = orbit.chat(self.a, [{"role": "user", "content": "how many hours yesterday on vscode"}], NOW)["reply"]
        self.assertIn("4 hours", y)
        self.assertEqual([orbit.duration_text(x) for x in (10, 49, 90, 3000, 3900, 7200)], ["less than a minute", "1 minute", "2 minutes", "50 minutes", "1 hour 5 minutes", "2 hours"])

    def test_commits_and_pull_requests_follow_task_data_in_demo_mode_only(self):
        from . import worksource
        from .models import TaskEvent
        yday = TODAY - timedelta(days=1)
        at = lambda d: datetime(d.year, d.month, d.day, 11, 0, tzinfo=NOW.tzinfo)
        done = Task.objects.create(asset=self.a, title="Fix login bug", status="done", percent=100, completed_at=at(yday))
        TaskEvent.objects.create(task=done, asset=self.a, at=at(yday), status="done", percent=100)
        wip = Task.objects.create(asset=self.a, title="Build report", status="in_progress", percent=60)
        TaskEvent.objects.create(task=wip, asset=self.a, at=at(TODAY), status="in_progress", percent=60)
        Task.objects.create(asset=self.b, title="Someone else's", status="done", percent=100, completed_at=at(yday))
        with override_settings(ORBIT_WORK_SOURCE="simulated"):
            _, impl = orbit.employee_tools(self.a, TODAY)
            r = impl["get_my_code_activity"]({"days": 3})
            self.assertEqual(r["source"], "demo data")
            self.assertIn("Fix login bug", r["byDay"][yday.isoformat()]["tasksCompleted"])
            self.assertGreaterEqual(r["byDay"][yday.isoformat()]["commits"], 1)
            self.assertLessEqual({("Fix login bug", "merged"), ("Build report", "open")}, {(p["title"], p["state"]) for p in r["pullRequests"]})
            quiet = worksource.code_activity(self.b, TODAY - timedelta(days=2), TODAY - timedelta(days=1), TODAY)
            self.assertGreaterEqual(len(quiet["commits"]), 3, "a working day with no task activity still has commits")
            again = worksource.code_activity(self.a, yday, TODAY, TODAY)
            self.assertEqual(again, worksource.code_activity(self.a, yday, TODAY, TODAY), "deterministic")
            reply = orbit.chat(self.a, [{"role": "user", "content": "How many commits did I make yesterday?"}], NOW)["reply"]
            self.assertIn("yesterday", reply.lower())
            self.assertIn("1 task", reply)
            self.assertIn("demo data", reply)
            self.assertIn("code", orbit.briefing(self.a, NOW)["sections"])
        with override_settings(ORBIT_WORK_SOURCE="off"):
            self.assertEqual(worksource.code_activity(self.a, yday, TODAY, TODAY), {"commits": [], "prs": []})
            self.assertFalse(worksource.code_connected())
            _, impl = orbit.employee_tools(self.a, TODAY)
            self.assertEqual(impl["get_my_code_activity"]({})["source"], "not connected")
            self.assertIn("don't have commit", orbit.chat(self.a, [{"role": "user", "content": "how many commits yesterday"}], NOW)["reply"])
