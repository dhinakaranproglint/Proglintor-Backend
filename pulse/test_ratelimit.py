from unittest import mock

from django.core.cache import cache
from django.core.checks import Error
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings

from . import apps, ratelimit


class RateLimitTests(TestCase):
    def setUp(self):
        cache.clear()

    def test_counts_attempts_in_a_window(self):
        results = [ratelimit.hit("k", 3, 60) for _ in range(5)]
        self.assertEqual(results, [False, False, False, True, True])
        self.assertFalse(ratelimit.hit("other-key", 3, 60), "keys are independent")

    def test_a_new_window_starts_clean(self):
        ratelimit.hit("k", 1, 60)
        self.assertTrue(ratelimit.hit("k", 1, 60))
        cache.delete("k")  # the window expired
        self.assertFalse(ratelimit.hit("k", 1, 60))

    def test_it_survives_a_window_expiring_between_add_and_incr(self):
        with mock.patch.object(ratelimit.cache, "incr", side_effect=ValueError):
            self.assertFalse(ratelimit.hit("k", 5, 60))
        self.assertEqual(cache.get("k"), 1)

    def test_a_broken_cache_fails_open_and_loudly(self):
        with mock.patch.object(ratelimit.cache, "add", side_effect=RuntimeError("cache down")), self.assertLogs("pulse.ratelimit", "ERROR"):
            self.assertFalse(ratelimit.hit("k", 1, 60), "sign-in must not go down because the cache did")


class ClientAddressTests(SimpleTestCase):
    def ip(self, forwarded=None, remote="10.0.0.9"):
        extra = {"REMOTE_ADDR": remote}
        if forwarded is not None:
            extra["HTTP_X_FORWARDED_FOR"] = forwarded
        return ratelimit.client_ip(RequestFactory().get("/", **extra))

    @override_settings(PULSE_TRUSTED_PROXIES=0)
    def test_without_a_proxy_the_header_is_ignored(self):
        self.assertEqual(self.ip("1.2.3.4"), "10.0.0.9", "a client cannot pick its own address")

    @override_settings(PULSE_TRUSTED_PROXIES=1)
    def test_behind_one_proxy_the_entry_our_proxy_added_is_used(self):
        self.assertEqual(self.ip("203.0.113.7"), "203.0.113.7")
        self.assertEqual(self.ip("6.6.6.6, 203.0.113.7"), "203.0.113.7", "an address the client typed in front is ignored")
        self.assertEqual(self.ip(None), "10.0.0.9")
        self.assertEqual(self.ip(""), "10.0.0.9")

    @override_settings(PULSE_TRUSTED_PROXIES=2)
    def test_behind_two_proxies(self):
        self.assertEqual(self.ip("6.6.6.6, 203.0.113.7, 10.1.1.1"), "203.0.113.7")
        self.assertEqual(self.ip("10.1.1.1"), "10.0.0.9", "fewer hops than expected: don't trust it")


class SystemCheckTests(TestCase):
    def test_a_working_cache_passes(self):
        self.assertEqual([e for e in apps.check_cache(None) if isinstance(e, Error)], [])

    @override_settings(DEBUG=False, RUNNING_TESTS=False)
    def test_per_process_cache_in_production_is_flagged(self):
        ids = [m.id for m in apps.check_cache(None)]
        self.assertIn("pulse.W001", ids)

    def test_a_cache_that_does_not_work_is_an_error(self):
        with mock.patch("django.core.cache.cache.set", side_effect=RuntimeError("no table")):
            self.assertIn("pulse.E001", [m.id for m in apps.check_cache(None)])
