from datetime import datetime, timedelta, timezone as dtz

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from . import mockdata, orbit
from .models import Asset, DailyUsage, Recognition, Task, WeekendAlert

TZ = dtz(timedelta(hours=5, minutes=30))
NOON = datetime(2026, 11, 18, 11, 0, tzinfo=TZ)     # a Wednesday, 11:00


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class MockDataTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.out = mockdata.seed(days=30, now=NOON)

    def test_a_month_of_people_with_all_the_pieces(self):
        self.assertEqual(Asset.objects.filter(asset_id__startswith="MOCK").count(), 8)
        self.assertGreater(self.out["usage"], 600)
        self.assertGreater(Task.objects.count(), 100)
        self.assertGreater(Recognition.objects.count(), 3)
        self.assertTrue(all(a.enrolled for a in Asset.objects.all()))
        days = set(DailyUsage.objects.values_list("day", flat=True))
        self.assertEqual(max(days), NOON.date())
        self.assertGreaterEqual(min(days), NOON.date() - timedelta(days=29))

    def test_working_days_look_like_working_days(self):
        asha = Asset.objects.get(asset_id="MOCK001")
        by_day = orbit.usage_by_day(asha, NOON.date() - timedelta(days=30))
        weekdays = [s for d, s in by_day.items() if d.weekday() < 5 and d < NOON.date()]
        weekends = [s for d, s in by_day.items() if d.weekday() >= 5]
        self.assertGreaterEqual(len(weekdays), 15)
        self.assertTrue(all(3 * 3600 <= s <= 9.6 * 3600 for s in weekdays), "6-ish hours on a normal day, never a 20-hour day")
        self.assertLess(len(weekends), len([d for d in by_day if d.weekday() >= 5]) + 1)
        self.assertTrue(all(s <= 7 * 3600 for s in weekends))

    def test_today_is_only_as_full_as_the_day_so_far(self):
        today = DailyUsage.objects.filter(day=NOON.date(), asset__asset_id="MOCK001")
        self.assertLess(sum(u.seconds for u in today), 3 * 3600, "at 11am there are only a couple of hours")
        early = datetime(2026, 11, 19, 7, 0, tzinfo=TZ)
        mockdata.seed(days=30, now=early)
        self.assertFalse(DailyUsage.objects.filter(day=early.date()).exists(), "nothing before the working day starts")

    def test_nothing_is_in_the_future(self):
        self.assertFalse(DailyUsage.objects.filter(day__gt=NOON.date()).exists())
        self.assertFalse(Task.objects.filter(completed_at__gt=NOON).exists())
        self.assertFalse(DailyUsage.objects.filter(last_active__gt=NOON).exists())

    def test_tasks_have_a_realistic_mix(self):
        by = {s: Task.objects.filter(status=s).count() for s in ("done", "in_progress", "todo", "blocked")}
        self.assertTrue(all(v > 0 for v in by.values()), by)
        self.assertGreater(by["done"], by["blocked"] * 3)
        overdue = Task.objects.exclude(status="done").filter(due_date__lt=NOON.date()).count()
        self.assertGreater(overdue, 0)
        early = [t for t in Task.objects.filter(status="done") if t.due_date and t.completed_at.date() < t.due_date]
        late = [t for t in Task.objects.filter(status="done") if t.due_date and t.completed_at.date() > t.due_date]
        self.assertTrue(early and late)
        self.assertTrue(all(t.percent == 100 for t in Task.objects.filter(status="done")))

    def test_recognitions_are_complete(self):
        approved = Recognition.objects.filter(status="approved")
        self.assertTrue(approved.exists())
        self.assertTrue(all(r.certificate_id.startswith("CERT-") and r.reward and r.decided_at for r in approved))
        self.assertTrue(Recognition.objects.filter(status="pending").exists())

    def test_weekend_work_leaves_alerts(self):
        self.assertTrue(WeekendAlert.objects.exists())
        for a in WeekendAlert.objects.all():
            self.assertGreaterEqual(datetime.fromisoformat(a.day).weekday(), 5)

    def test_rerunning_refreshes_without_duplicating(self):
        counts = (DailyUsage.objects.count(), Task.objects.count(), Recognition.objects.count(), WeekendAlert.objects.count())
        again = mockdata.seed(days=30, now=NOON)
        self.assertEqual((DailyUsage.objects.count(), Task.objects.count(), Recognition.objects.count(), WeekendAlert.objects.count()), counts)
        self.assertEqual((again["tasks"], again["recognitions"], again["alerts"]), (0, 0, 0))

    def test_the_same_day_always_looks_the_same(self):
        first = list(DailyUsage.objects.filter(asset__asset_id="MOCK002").order_by("day", "app_id").values_list("seconds", flat=True))
        DailyUsage.objects.all().delete()
        mockdata.seed(days=30, now=NOON)
        self.assertEqual(first, list(DailyUsage.objects.filter(asset__asset_id="MOCK002").order_by("day", "app_id").values_list("seconds", flat=True)))

    def test_devices_look_live_during_work_hours(self):
        a = Asset.objects.get(asset_id="MOCK001")
        self.assertEqual(a.state, "tracking")
        self.assertTrue(a.current_app_name)
        self.assertEqual(a.platform, "darwin")
        self.assertTrue(a.installed.apps)

    def test_orbit_works_on_the_mock_people(self):
        asha = Asset.objects.get(asset_id="MOCK001")
        b = orbit.briefing(asha, NOON)
        self.assertEqual(b["greeting"]["text"], "Good morning, Asha!")
        self.assertTrue(b["message"])
        self.assertEqual(len(orbit.achievements(asha, NOON.date())), 5)
        self.assertTrue(any(m["earned"] for m in orbit.achievements(asha, NOON.date())))

    def test_removing_only_touches_mock_people(self):
        real = Asset.objects.create(asset_id="REAL1", employee_name="Real Person", employee_email="r@x.co")
        DailyUsage.objects.create(asset=real, day=NOON.date(), app_id="x", app_name="X", seconds=60)
        self.assertEqual(mockdata.remove_mock(), 8)
        self.assertEqual(Asset.objects.count(), 1)
        self.assertEqual(DailyUsage.objects.count(), 1)

    def test_a_real_laptop_only_gets_mock_tasks_never_mock_usage(self):
        real = Asset.objects.create(asset_id="REAL1", employee_name="Real Person", employee_email="r@x.co")
        out = mockdata.seed(days=14, now=NOON, assets=[real])
        real.refresh_from_db()
        self.assertEqual((real.employee_name, out["assets"]), ("Real Person", 1))
        self.assertFalse(DailyUsage.objects.filter(asset=real).exists(), "app usage on a real laptop is only ever what it really reported")
        self.assertFalse(WeekendAlert.objects.filter(asset=real).exists())
        self.assertTrue(Task.objects.filter(asset=real).exists())
        self.assertEqual(Asset.objects.filter(asset_id__startswith="MOCK").count(), 8, "the earlier mock people are untouched")

    def test_the_command_runs(self):
        call_command("seed_mock_data", "--days", "10", stdout=__import__("io").StringIO())
        self.assertEqual(Asset.objects.filter(asset_id__startswith="MOCK").count(), 8)


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off")
class FewMockDataTests(TestCase):
    def test_a_small_tidy_demo_set(self):
        out = mockdata.seed(days=14, now=NOON, few=True)
        self.assertEqual(Asset.objects.filter(asset_id__startswith="MOCK").count(), 3)
        self.assertEqual(Task.objects.count(), 18)
        self.assertEqual(Recognition.objects.filter(status="pending").count(), 6)
        self.assertEqual(Recognition.objects.filter(status="approved").count(), 3)
        self.assertTrue(all(r.certificate_id for r in Recognition.objects.filter(status="approved")))
        self.assertEqual(out["assets"], 3)
        self.assertGreater(Task.objects.exclude(status="done").count(), 0, "the admin sees open work")
