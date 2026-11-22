"""Timesheets, calendar, AI summaries and Employee of the Month. No real model or network is used."""
import json
from datetime import date, datetime, time, timedelta
from unittest import mock

from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from . import mockdata, orbit, timesheet
from .models import Asset, DailyUsage, MonthlyAward, Recognition, Task, TaskEvent, TimesheetSummary

TODAY = date(2026, 3, 18)          # a Wednesday
FIRST = date(2026, 3, 1)


def at(day, hour=11):
    return timezone.make_aware(datetime.combine(day, time(hour, 0)))


def frozen(today=TODAY):
    return mock.patch.object(timesheet.timezone, "localdate", return_value=today)


class LevelTests(SimpleTestCase):
    def test_levels_and_months(self):
        self.assertEqual([timesheet.level(h) for h in (0, 0.5, 2, 4, 6.9, 7, 11)], [0, 1, 2, 3, 3, 4, 4])
        self.assertEqual(timesheet.parse_month("2026-11"), date(2026, 11, 1))
        self.assertEqual(timesheet.month_end(date(2026, 2, 1)), date(2026, 2, 28))
        self.assertEqual(timesheet.week_start(date(2026, 3, 18)), date(2026, 3, 16))
        for bad in ("2026-13", "26-1", "november", 5):
            with self.assertRaises(orbit.OrbitError):
                timesheet.parse_month(bad)


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class TimesheetTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="a@x.co", enrolled_at=timezone.now())
        self.b = Asset.objects.create(asset_id="B1", employee_name="Bala K", employee_email="b@x.co", enrolled_at=timezone.now())

    def use(self, asset, day, hours, app="VS Code"):
        DailyUsage.objects.create(asset=asset, day=day, app_id=app.lower(), app_name=app, seconds=hours * 3600)

    def task(self, asset, title, **kw):
        return Task.objects.create(asset=asset, title=title, **kw)

    def ev(self, task, day, status="in_progress", pct=50, hour=11):
        return TaskEvent.objects.create(task=task, asset=task.asset, at=at(day, hour), status=status, percent=pct)

    def test_a_day_combines_real_tracked_time_with_task_activity(self):
        t1, t2 = self.task(self.a, "Fix login"), self.task(self.a, "Write docs")
        self.use(self.a, date(2026, 3, 10), 6)
        self.use(self.a, date(2026, 3, 10), 2, "Chrome")
        self.ev(t1, date(2026, 3, 10), "in_progress", 40)
        self.ev(t2, date(2026, 3, 10), "done", 100)
        with frozen():
            d = next(x for x in timesheet.timesheet(self.a, FIRST, date(2026, 3, 31))["days"] if x["date"] == "2026-03-10")
        self.assertEqual(d["activeHours"], 8.0)
        self.assertEqual([a["app"] for a in d["topApps"]], ["VS Code", "Chrome"])
        self.assertEqual(sorted(w["title"] for w in d["tasksWorked"]), ["Fix login", "Write docs"])
        self.assertTrue(all(w["hours"] == 4.0 and w["estimated"] for w in d["tasksWorked"]), "8 hours split over 2 tasks, labelled as an estimate")
        self.assertEqual(d["tasksDone"], ["Write docs"])
        self.assertEqual(d["unassignedHours"], 0)

    def test_time_with_no_task_activity_is_unassigned_not_invented(self):
        self.use(self.a, date(2026, 3, 11), 5)
        with frozen():
            d = next(x for x in timesheet.timesheet(self.a, FIRST, date(2026, 3, 31))["days"] if x["date"] == "2026-03-11")
        self.assertEqual((d["tasksWorked"], d["unassignedHours"]), ([], 5.0))

    def test_only_the_last_thing_that_happened_to_a_task_that_day_counts(self):
        t = self.task(self.a, "Ship it")
        self.use(self.a, date(2026, 3, 12), 4)
        self.ev(t, date(2026, 3, 12), "in_progress", 20, hour=9)
        self.ev(t, date(2026, 3, 12), "done", 100, hour=16)
        with frozen():
            d = next(x for x in timesheet.timesheet(self.a, FIRST, date(2026, 3, 31))["days"] if x["date"] == "2026-03-12")
        self.assertEqual((len(d["tasksWorked"]), d["tasksWorked"][0]["status"], d["tasksDone"]), (1, "done", ["Ship it"]))

    def test_days_after_today_are_empty_and_marked(self):
        self.use(self.a, date(2026, 3, 25), 6)       # impossible data in the future is ignored
        with frozen():
            ts = timesheet.timesheet(self.a, FIRST, date(2026, 3, 31))
        d = next(x for x in ts["days"] if x["date"] == "2026-03-25")
        self.assertTrue(d["future"])
        self.assertEqual(d["activeHours"], 0)

    def test_totals(self):
        for i, h in enumerate((6, 7, 5, 4, 6)):      # Mon 9th to Fri 13th
            self.use(self.a, date(2026, 3, 9) + timedelta(days=i), h)
        self.use(self.a, date(2026, 3, 14), 5)       # Saturday
        self.use(self.a, date(2026, 3, 16), 11)      # a very long Monday
        done = self.task(self.a, "On time", due_date=date(2026, 3, 12), status="done", percent=100)
        late = self.task(self.a, "Late", due_date=date(2026, 3, 10), status="done", percent=100)
        Task.objects.filter(pk=done.pk).update(completed_at=at(date(2026, 3, 11)))
        Task.objects.filter(pk=late.pk).update(completed_at=at(date(2026, 3, 13)))
        self.task(self.a, "Still open and late", due_date=date(2026, 3, 5))
        with frozen():
            tot = timesheet.timesheet(self.a, FIRST, date(2026, 3, 31))["totals"]
        self.assertEqual(tot["activeHours"], 44.0)
        self.assertEqual(tot["workdays"], 6)
        self.assertEqual(tot["weekendHours"], 5.0)
        self.assertEqual(tot["longestStreak"], 6, "Friday and the next Monday are consecutive working days")
        self.assertEqual(tot["veryLongDays"], 1)
        self.assertEqual((tot["tasksCompleted"], tot["onTimeRate"]), (2, 50))
        self.assertEqual(tot["openOverdue"], 1)
        self.assertEqual(tot["avgHoursPerWorkday"], round((6 + 7 + 5 + 4 + 6 + 11) / 6, 1))

    def test_everything_is_scoped_to_one_person(self):
        self.use(self.b, date(2026, 3, 10), 9, "Secret app")
        self.ev(self.task(self.b, "Bala only"), date(2026, 3, 10))
        with frozen():
            out = json.dumps(timesheet.timesheet(self.a, FIRST, date(2026, 3, 31)))
        self.assertNotIn("Secret app", out)
        self.assertNotIn("Bala only", out)

    def test_meetings_come_from_the_work_source(self):
        with override_settings(ORBIT_WORK_SOURCE="simulated"), frozen():
            ts = timesheet.timesheet(self.a, date(2026, 3, 9), date(2026, 3, 13))
        self.assertGreater(ts["totals"]["meetingHours"], 0)

    def test_task_changes_are_recorded(self):
        t = self.task(self.a, "Tracked", status="todo")
        timesheet.record_event(t)
        t.status, t.percent = "in_progress", 30
        t.save()
        timesheet.record_event(t)
        self.assertEqual(list(TaskEvent.objects.filter(task=t).values_list("status", "percent")), [("todo", 0), ("in_progress", 30)])


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class SummaryTests(TestCase):
    def setUp(self):
        cache.clear()
        self.a = Asset.objects.create(asset_id="A1", employee_name="Asha Rao", employee_email="a@x.co", enrolled_at=timezone.now())
        for i in range(5):
            DailyUsage.objects.create(asset=self.a, day=date(2026, 3, 9) + timedelta(days=i), app_id="c", app_name="VS Code", seconds=6 * 3600)
        t = Task.objects.create(asset=self.a, title="Ship the report", status="done", percent=100, due_date=date(2026, 3, 13))
        Task.objects.filter(pk=t.pk).update(completed_at=at(date(2026, 3, 11)))
        TaskEvent.objects.create(task=t, asset=self.a, at=at(date(2026, 3, 11)), status="done", percent=100)

    def test_template_summary_states_real_numbers(self):
        with frozen():
            s = timesheet.summary(self.a, "month", FIRST)
        self.assertEqual(s["source"], "template")
        self.assertIn("30.0 hours", s["text"])
        self.assertIn("5 working days", s["text"])
        self.assertIn("Ship the report", s["text"])
        self.assertEqual(s["totals"]["tasksCompleted"], 1)

    def test_it_is_stored_and_reused_until_the_data_changes(self):
        reply = {"candidates": [{"content": {"parts": [{"text": "A steady month: 30 hours and the report shipped."}]}}]}
        with override_settings(GEMINI_API_KEY="fake"), frozen(), mock.patch("pulse.assistant._post", return_value=reply) as post:
            first = timesheet.summary(self.a, "month", FIRST)
            again = timesheet.summary(self.a, "month", FIRST)
            self.assertEqual((first["source"], first["text"]), ("llm", "A steady month: 30 hours and the report shipped."))
            self.assertEqual(again["text"], first["text"])
            self.assertEqual(post.call_count, 1, "written once; the employee and the admin read the same words")
            self.assertEqual(TimesheetSummary.objects.count(), 1)
            DailyUsage.objects.create(asset=self.a, day=date(2026, 3, 16), app_id="c", app_name="VS Code", seconds=3 * 3600)
            timesheet.summary(self.a, "month", FIRST)
            self.assertEqual(post.call_count, 2, "new data, new summary")
        sent = json.dumps(post.call_args[0][0])
        self.assertNotIn("a@x.co", sent)

    def test_a_model_failure_falls_back_to_the_template(self):
        with override_settings(GEMINI_API_KEY="fake"), frozen(), mock.patch("pulse.assistant._post", return_value=None):
            self.assertEqual(timesheet.summary(self.a, "week", date(2026, 3, 9))["source"], "template")

    def test_weekly_and_empty_periods(self):
        with frozen():
            w = timesheet.summary(self.a, "week", date(2026, 3, 9))
            empty = timesheet.summary(self.a, "week", date(2026, 1, 5))
        self.assertIn("30.0 hours", w["text"])
        self.assertIn("don't see any tracked time", empty["text"])


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class AwardTests(TestCase):
    def setUp(self):
        cache.clear()
        self.admin = User.objects.create_user("boss", is_staff=True)
        self.p = {n: Asset.objects.create(asset_id=n, employee_name=f"{n} Person", employee_email=f"{n}@x.co", enrolled_at=timezone.now(), department="Eng") for n in ("A", "B", "C", "D")}
        self.start = date(2026, 3, 2)

    def work(self, asset, hours, days=12, **kw):
        d = self.start
        n = 0
        while n < days:
            if d.weekday() < 5:
                DailyUsage.objects.create(asset=asset, day=d, app_id="c", app_name="Code", seconds=hours * 3600)
                n += 1
            d += timedelta(days=1)

    def done(self, asset, count, priority="medium", late=0):
        for i in range(count):
            due = date(2026, 3, 10 + i % 5)
            t = Task.objects.create(asset=asset, title=f"{asset.asset_id} task {i}", priority=priority, status="done", percent=100, due_date=due)
            Task.objects.filter(pk=t.pk).update(completed_at=at(due + timedelta(days=3 if i < late else -1)))

    def recognise(self, asset, n, credits=1):
        for i in range(n):
            Recognition.objects.create(asset=asset, kind="extra", title=f"Win {i}", status="approved", credits=credits, decided_at=at(date(2026, 3, 12)))

    def build(self):
        self.work(self.p["A"], 6); self.done(self.p["A"], 8, "high"); self.recognise(self.p["A"], 2)         # the all-rounder
        self.work(self.p["B"], 11); self.done(self.p["B"], 9, "high")                                        # works very long days
        self.work(self.p["C"], 6); self.done(self.p["C"], 2, "low", late=2)                                  # quiet and late
        Asset.objects.filter(asset_id="D").update(enrolled_at=timezone.now())                                # no data at all

    def test_ranking_is_explainable_and_ignores_people_with_no_data(self):
        self.build()
        with frozen(date(2026, 3, 31)):
            c = timesheet.month_candidates(FIRST)
        self.assertEqual([x["assetId"] for x in c], ["A", "B", "C"], "D has nothing recorded this month")
        top = c[0]
        self.assertEqual(top["rank"], 1)
        self.assertEqual(set(top["breakdown"]), {"delivery", "reliability", "consistency", "appreciation"})
        self.assertTrue(all(0 <= v["score"] <= 100 and v["detail"] for v in top["breakdown"].values()))
        self.assertEqual(top["recognitions"][0]["kind"], "extra")
        self.assertIn("tasks delivered", top["suggestedCitation"])

    def test_overwork_is_flagged_and_never_rewarded(self):
        self.build()
        with frozen(date(2026, 3, 31)):
            c = {x["assetId"]: x for x in timesheet.month_candidates(FIRST)}
        self.assertTrue(any("very long day" in f for f in c["B"]["flags"]))
        self.assertEqual(c["B"]["breakdown"]["consistency"]["score"], 0, "11-hour days are not 'healthy' days")
        self.assertGreater(c["A"]["score"], c["B"]["score"], "more tasks but unhealthy hours do not beat steady, appreciated work")

    def test_appreciation_counts_only_this_month_and_never_the_monthly_award_itself(self):
        self.build()
        Recognition.objects.create(asset=self.p["C"], kind="extra", title="Old", status="approved", credits=5, decided_at=at(date(2026, 2, 10)))
        Recognition.objects.create(asset=self.p["C"], kind="month", title="Employee of the Month: Feb", status="approved", credits=2, decided_at=at(date(2026, 3, 3)))
        with frozen(date(2026, 3, 31)):
            c = {x["assetId"]: x for x in timesheet.month_candidates(FIRST)}
        self.assertEqual(c["C"]["recognitions"], [])

    def test_choosing_the_employee_of_the_month(self):
        self.build()
        with frozen(date(2026, 3, 31)):
            award = timesheet.award_month(self.admin, self.p["A"], FIRST, "Extra day off", 2.0, "")
        rec = award.recognition
        self.assertEqual((rec.kind, rec.status, rec.credits, rec.reward), ("month", "approved", 2.0, "Extra day off"))
        self.assertTrue(rec.certificate_id.startswith("CERT-"))
        self.assertEqual(rec.title, "Employee of the Month: March 2026")
        self.assertIn("tasks delivered", rec.citation)
        self.assertEqual(award.scores["assetId"], "A")
        self.assertEqual(orbit.credits_total(self.p["A"]), 4.0, "the 2 credits count like any other recognition, on top of the 2 from earlier recognitions")
        with self.assertRaises(orbit.OrbitError) as cm:
            with frozen(date(2026, 3, 31)):
                timesheet.award_month(self.admin, self.p["B"], FIRST, "Gift card")
        self.assertEqual(cm.exception.status, 409)

    def test_replacing_the_winner_removes_the_old_certificate(self):
        self.build()
        with frozen(date(2026, 3, 31)):
            first = timesheet.award_month(self.admin, self.p["A"], FIRST, "Gift card")
            old = first.recognition_id
            timesheet.award_month(self.admin, self.p["B"], FIRST, "Comp-off", replace=True)
        self.assertEqual(MonthlyAward.objects.get(month="2026-03").asset_id, "B")
        self.assertFalse(Recognition.objects.filter(pk=old).exists())
        self.assertEqual(Recognition.objects.filter(kind="month").count(), 1)

    def test_a_month_that_has_not_started_cannot_be_awarded(self):
        with frozen(date(2026, 3, 31)), self.assertRaises(orbit.OrbitError) as cm:
            timesheet.award_month(self.admin, self.p["A"], date(2026, 5, 1), "x")
        self.assertEqual(cm.exception.status, 422)

    def test_the_ai_citation_is_grounded_and_falls_back(self):
        self.build()
        with frozen(date(2026, 3, 31)):
            cand = timesheet.month_candidates(FIRST)[0]
            self.assertEqual(timesheet.ai_citation(cand, "March 2026")[1], "template")
            reply = {"candidates": [{"content": {"parts": [{"text": "A superb month of dependable delivery."}]}}]}
            with override_settings(GEMINI_API_KEY="fake"), mock.patch("pulse.assistant._post", return_value=reply) as post:
                text, source = timesheet.ai_citation(cand, "March 2026")
        self.assertEqual((text, source), ("A superb month of dependable delivery.", "llm"))
        self.assertNotIn("@x.co", json.dumps(post.call_args[0][0]))


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
        self.today = timezone.localdate()
        for aid, hrs in (("PAP1", 6), ("PAP2", 9)):
            DailyUsage.objects.create(asset_id=aid, day=self.today - timedelta(days=1), app_id="c", app_name="Code" if aid == "PAP1" else "Secret", seconds=hrs * 3600)

    def test_roles_are_separated(self):
        self.assertEqual(APIClient().get("/api/agent/orbit/timesheet").status_code, 401)
        self.assertEqual(self.admin.get("/api/agent/orbit/timesheet").status_code, 401, "an admin token isn't an agent token")
        self.assertEqual(self.me.get("/api/admin/orbit/timesheets/PAP2").status_code, 401, "an employee can never read someone else's timesheet")
        self.assertEqual(self.me.get("/api/admin/orbit/awards/candidates").status_code, 401)
        self.assertEqual(self.me.post("/api/admin/orbit/awards", {}, format="json").status_code, 401)

    def test_an_employee_sees_only_their_own_month(self):
        r = self.me.get("/api/agent/orbit/timesheet").json()
        self.assertEqual(r["month"], self.today.strftime("%Y-%m"))
        self.assertNotIn("Secret", json.dumps(r))
        self.assertEqual(self.me.get("/api/agent/orbit/timesheet", {"month": "nope"}).status_code, 422)
        self.assertEqual(self.me.get("/api/agent/orbit/timesheet", {"month": "2025-01"}).json()["totals"]["activeHours"], 0)

    def test_the_admin_sees_any_employee(self):
        r = self.admin.get("/api/admin/orbit/timesheets/PAP2").json()
        self.assertEqual((r["assetId"], r["employeeName"]), ("PAP2", "Emp PAP2"))
        self.assertIn("Secret", json.dumps(r))
        self.assertEqual(self.admin.get("/api/admin/orbit/timesheets/NOPE").status_code, 404)

    def test_summary_is_the_same_for_both(self):
        e = self.me.get("/api/agent/orbit/timesheet/summary").json()
        a = self.admin.get("/api/admin/orbit/timesheets/PAP1/summary").json()
        self.assertEqual(e["text"], a["text"])
        self.assertEqual(self.me.get("/api/agent/orbit/timesheet/summary", {"period": "year"}).status_code, 422)
        self.assertEqual(self.me.get("/api/agent/orbit/timesheet/summary", {"start": "soon"}).status_code, 422)

    def test_awarding_end_to_end(self):
        month = self.today.strftime("%Y-%m")
        cands = self.admin.get("/api/admin/orbit/awards/candidates", {"month": month}).json()
        self.assertTrue(cands["candidates"])
        self.assertIn("you decide", cands["method"])
        cit = self.admin.post("/api/admin/orbit/awards/citation", {"month": month, "assetId": "PAP1"}, format="json").json()
        self.assertEqual(cit["source"], "template")
        self.assertEqual(self.admin.post("/api/admin/orbit/awards/citation", {"month": month, "assetId": "NOPE"}, format="json").status_code, 404)
        made = self.admin.post("/api/admin/orbit/awards", {"month": month, "assetId": "PAP1", "reward": "Extra day off", "credits": 2, "citation": cit["citation"]}, format="json")
        self.assertEqual(made.status_code, 201)
        self.assertEqual(self.admin.post("/api/admin/orbit/awards", {"month": month, "assetId": "PAP2", "reward": "x"}, format="json").status_code, 409)
        self.assertEqual(self.admin.post("/api/admin/orbit/awards", {"month": month, "assetId": "PAP2", "reward": "x", "replace": True}, format="json").status_code, 201)
        hist = self.admin.get("/api/admin/orbit/awards").json()["items"]
        self.assertEqual([h["assetId"] for h in hist], ["PAP2"])
        # the winner sees it as a normal certificate; the other person does not
        recs = self.other.get("/api/agent/orbit/achievements").json()["recognitions"]
        win = next(r for r in recs if r["kind"] == "month")
        self.assertEqual(win["status"], "approved")
        self.assertIn("html", self.other.get(f"/api/agent/orbit/recognitions/{win['id']}/certificate").json())
        self.assertFalse([r for r in self.me.get("/api/agent/orbit/achievements").json()["recognitions"] if r["kind"] == "month"])
        self.assertEqual(self.admin.post("/api/admin/orbit/awards", {"month": "2099-01", "assetId": "PAP1"}, format="json").status_code, 422)

    def test_task_changes_feed_the_timesheet(self):
        tid = self.admin.post("/api/admin/orbit/tasks", {"title": "Do the thing", "priority": "high", "estimatedHours": 4, "assetIds": ["PAP1"]}, format="json").json()["items"][0]["id"]
        self.me.patch(f"/api/agent/orbit/tasks/{tid}", {"status": "in_progress", "percent": 40}, format="json")
        self.admin.patch(f"/api/admin/orbit/tasks/{tid}", {"status": "done"}, format="json")
        self.assertEqual(list(TaskEvent.objects.filter(task_id=tid).values_list("status", "percent")), [("todo", 0), ("in_progress", 40), ("done", 100)])

    def test_chat_tool_returns_only_my_timesheet(self):
        _, impl = orbit.employee_tools(Asset.objects.get(asset_id="PAP1"), self.today)
        out = json.dumps(impl["get_my_timesheet"]({"days": 7}), default=str)
        self.assertIn("6.0", out)
        self.assertNotIn("Secret", out)


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class MockTimesheetTests(TestCase):
    def test_the_mock_people_have_real_looking_timesheets(self):
        now = timezone.localtime()
        mockdata.seed(days=30, now=now, few=True)
        a = Asset.objects.get(asset_id="MOCK001")
        self.assertTrue(TaskEvent.objects.filter(asset=a).exists())
        ts = timesheet.month_timesheet(a, now.date().replace(day=1))
        self.assertGreater(ts["totals"]["activeHours"] + ts["totals"]["tasksCompleted"], 0)
        prev = (now.date().replace(day=1) - timedelta(days=1)).replace(day=1)
        self.assertTrue(timesheet.month_candidates(prev) or timesheet.month_candidates(now.date().replace(day=1)))


class CleanReplyTests(SimpleTestCase):
    def test_long_text_is_cut_at_a_sentence_not_mid_word(self):
        from . import assistant
        text = "First sentence is here. " * 12 + "A final thought that goes on and on"
        out = assistant.clean_reply(text, 100)
        self.assertTrue(out.endswith("."), out)
        self.assertLessEqual(len(out), 100)
        self.assertEqual(assistant.clean_reply("short and sweet.", 100), "short and sweet.")
        self.assertTrue(assistant.clean_reply("word " * 100, 50).endswith("…"), "no sentence to cut at: falls back to a word boundary")
