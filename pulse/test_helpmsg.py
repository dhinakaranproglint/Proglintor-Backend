"""The employee says they have no task: their reporting person (or, failing that, the admin) is told. No model or mail server is used."""
from datetime import date, datetime

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from . import helpmsg, orbit
from .models import Asset, ManagerMessage, Task


@override_settings(GEMINI_API_KEY="", ORBIT_WORK_SOURCE="off", EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
class HelpMessageTests(TestCase):
    def setUp(self):
        self.a = Asset.objects.create(asset_id="H1", employee_name="Hana Ito", employee_email="h@x.com", manager_name="Raj Rao", manager_email="raj@x.com")

    def test_message_goes_to_the_reporting_person(self):
        r = helpmsg.request_work(self.a, "happy to take anything")
        self.assertTrue(r["sent"])
        self.assertEqual(mail.outbox[0].to, ["raj@x.com"])
        self.assertIn("Hana Ito", mail.outbox[0].body)
        self.assertIn("happy to take anything", mail.outbox[0].body)
        self.assertTrue(ManagerMessage.objects.get().emailed)

    def test_refused_when_they_actually_have_tasks(self):
        Task.objects.create(asset=self.a, title="Fix login")
        r = helpmsg.request_work(self.a)
        self.assertFalse(r["sent"])
        self.assertEqual(r["reason"], "has_tasks")
        self.assertEqual(len(mail.outbox), 0)

    def test_done_tasks_do_not_count_and_only_one_message_a_day(self):
        Task.objects.create(asset=self.a, title="Old", status="done")
        self.assertTrue(helpmsg.request_work(self.a)["sent"])
        again = helpmsg.request_work(self.a)
        self.assertFalse(again["sent"])
        self.assertEqual(again["reason"], "already_sent")
        self.assertEqual(len(mail.outbox), 1)

    def test_no_reporting_person_falls_back_to_the_admins(self):
        self.a.manager_email = ""
        self.a.save()
        User.objects.create_user("boss", email="boss@x.com", password="x", is_staff=True)
        r = helpmsg.request_work(self.a)
        self.assertEqual((r["to"], mail.outbox[0].to), ("the admin", ["boss@x.com"]))

    def test_mail_failure_keeps_the_message_for_the_admin_inbox(self):
        from unittest import mock
        with mock.patch.object(helpmsg, "send_mail", side_effect=OSError("down")):
            r = helpmsg.request_work(self.a)
        self.assertTrue(r["sent"] and not r["emailed"])
        self.assertEqual(ManagerMessage.objects.count(), 1)

    def test_note_is_cleaned(self):
        helpmsg.request_work(self.a, "see https://evil.example now **please**")
        body = ManagerMessage.objects.get().body
        self.assertNotIn("http", body)
        self.assertNotIn("**", body)

    def test_chat_without_a_model_does_it_from_plain_words(self):
        out = orbit.chat(self.a, [{"role": "user", "content": "I don't have any task right now"}], datetime(2026, 3, 18, 10, 0))
        self.assertIn("Raj Rao", out["reply"])
        self.assertEqual(ManagerMessage.objects.count(), 1)

    def test_assigning_work_resolves_it_and_the_admin_inbox_lists_it(self):
        helpmsg.request_work(self.a)
        admin = User.objects.create_user("adm", password="x", is_staff=True)
        c = APIClient()
        c.force_authenticate(admin)
        self.assertEqual(c.get("/api/admin/orbit/messages?open=1").json()["open"], 1)
        r = c.post("/api/admin/orbit/tasks", {"assetIds": ["H1"], "title": "New thing", "priority": "medium", "estimatedHours": 2}, format="json")
        self.assertEqual(r.status_code, 201, r.content)
        self.assertEqual(c.get("/api/admin/orbit/messages?open=1").json()["open"], 0)
