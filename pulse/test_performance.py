"""Guards against the admin lists getting slow as history grows (they are refreshed every few seconds by every open admin app)."""
import base64

from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from .models import Asset, SoftwareItem, SoftwareRequest

BIG_ICON = "data:image/png;base64," + base64.b64encode(b"x" * 100_000).decode()  # far bigger than a real icon


class AdminListCostTests(TestCase):
    def setUp(self):
        User.objects.create_user("a", password="p", is_staff=True)
        self.c = APIClient()
        tok = self.c.post("/api/admin/login", {"username": "a", "password": "p"}, format="json").json()["token"]
        self.c.credentials(HTTP_AUTHORIZATION=f"Token {tok}")
        self.assets = [Asset.objects.create(asset_id=f"A{i}", employee_name=f"E{i}", employee_email=f"e{i}@x.co", platform="darwin") for i in range(5)]
        self.item = SoftwareItem.objects.create(slug="s", name="S", version="1", platform="darwin", installer_type="dmg", download_url="https://x.example/a.dmg",
                                                sha256="a" * 64, icon=BIG_ICON)

    made = 0

    def make(self, n, status):
        for _ in range(n):
            self.made += 1
            i = self.made
            SoftwareRequest.objects.create(asset=self.assets[i % 5], idempotency_key=f"{status}-{i}", raw_request="x", software=self.item, software_name="S", status=status)

    def measure(self, url):
        with CaptureQueriesContext(connection) as q:
            body = self.c.get(url).content
        return len(q), len(body)

    def test_history_costs_the_same_however_long_it_is(self):
        self.make(5, "SUCCEEDED")
        few_q, _ = self.measure("/api/admin/software/requests")
        self.make(95, "SUCCEEDED")
        many_q, size = self.measure("/api/admin/software/requests")
        self.assertEqual(many_q, few_q, "no extra queries per row")
        self.assertLess(many_q, 12)
        self.assertLess(size, 150 * 1024, f"100 rows must not carry 100 copies of an icon (was {size // 1024} KB)")

    def test_the_catalogue_list_does_not_resend_every_icon_each_time(self):
        for i in range(20):
            SoftwareItem.objects.create(slug=f"i{i}", name=f"I{i}", version="1", platform="darwin", installer_type="dmg", download_url="https://x.example/a.dmg",
                                        sha256="a" * 64, icon=BIG_ICON)
        q, size = self.measure("/api/admin/software/items")
        self.assertLess(q, 6)
        self.assertLess(size, 40 * 1024, f"was {size // 1024} KB for 21 entries")

    def test_pending_approvals_still_show_icons_with_a_constant_number_of_queries(self):
        self.make(3, "AWAITING_APPROVAL")
        few_q, _ = self.measure("/api/admin/software/approvals")
        self.make(30, "AWAITING_APPROVAL")
        many_q, _ = self.measure("/api/admin/software/approvals")
        self.assertEqual(many_q, few_q)
        row = self.c.get("/api/admin/software/approvals").json()["items"][0]
        self.assertEqual(row["icon"], BIG_ICON)
