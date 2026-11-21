"""Real concurrency (separate database connections), not simulated: a double click or a retried slow request must never
produce a server error or a duplicate request."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest import mock

from django.db import connection
from django.test import TransactionTestCase, override_settings

from . import software as sw
from .models import Asset, SoftwareItem, SoftwareRequest

THREADS = 8


@override_settings(GEMINI_API_KEY="")
class ConcurrentRequestTests(TransactionTestCase):
    def setUp(self):
        self.asset = Asset.objects.create(asset_id="PAP1", employee_name="E", employee_email="e@x.co", platform="darwin", arch="arm64")
        SoftwareItem.objects.create(slug="git", name="Git", version="1.0", platform="darwin", installer_type="zip", download_url="https://dl.example.com/g.zip",
                                    sha256="a" * 64, requires_approval=False)

    def race(self, keys):
        barrier = Barrier(len(keys))

        def go(key):
            try:
                barrier.wait(timeout=10)  # everyone starts together
                return sw.create_request(self.asset, "install git", key)
            except Exception as err:  # noqa: BLE001
                return err
            finally:
                connection.close()

        with ThreadPoolExecutor(len(keys)) as pool:
            return list(pool.map(go, keys))

    def test_the_same_request_sent_many_times_at_once_creates_one_request_and_no_errors(self):
        results = self.race(["same-key"] * THREADS)
        errors = [r for r in results if isinstance(r, Exception)]
        self.assertEqual(errors, [], "no request may fail")
        self.assertEqual(SoftwareRequest.objects.count(), 1)
        self.assertEqual(len({r[0].pk for r in results}), 1, "everyone is shown the same request")
        self.assertEqual(sum(1 for r in results if not r[1]), 1, "exactly one of them created it")

    def test_the_same_software_asked_for_under_different_ids_at_once_is_still_one_request(self):
        results = self.race([f"key-{i}" for i in range(THREADS)])
        self.assertEqual([r for r in results if isinstance(r, Exception)], [])
        self.assertEqual(SoftwareRequest.objects.filter(status__in=("QUEUED", "AWAITING_APPROVAL")).count(), 1, "no twin requests for one laptop and app")

    def test_the_lock_is_what_makes_this_safe(self):
        """Guards the test itself: without the per-laptop lock the same race produces duplicates or errors."""
        with mock.patch.object(sw, "_lock_asset", lambda asset: None):
            results = self.race([f"key-{i}" for i in range(THREADS)])
        broken = [r for r in results if isinstance(r, Exception)] or SoftwareRequest.objects.filter(status="QUEUED").count() > 1
        self.assertTrue(broken, "this race is real: removing the lock must make it visible")
