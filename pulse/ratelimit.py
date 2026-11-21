"""Rate limiting and client addresses, shared by every endpoint that throttles.

Counters live in Django's cache. In production that cache must be shared between gunicorn workers (see CACHE_BACKEND in
config/settings.py: the database cache works out of the box), otherwise every worker counts on its own and a limit of
10 attempts really allows 10 x workers. If the cache itself is down, throttling fails open (and logs loudly) rather than
taking sign-in and the install flow down with it.
"""
import logging

from django.conf import settings
from django.core.cache import cache

log = logging.getLogger(__name__)


def hit(key, limit, window):
    """Counts one attempt for `key` (a fixed window of `window` seconds). True when the attempt is over the limit."""
    try:
        cache.add(key, 0, window)  # no-op when the window is already running
        try:
            count = cache.incr(key)
        except ValueError:  # the window expired between add() and incr()
            cache.set(key, 1, window)
            count = 1
        return count > limit
    except Exception:  # noqa: BLE001 - any cache failure
        log.exception("Rate-limit cache unavailable; allowing the request")
        return False


def client_ip(request):
    """The caller's address. X-Forwarded-For is only believed when the app sits behind that many trusted proxies
    (PULSE_TRUSTED_PROXIES): the entry our own proxy appended, not a value the client typed in front of it."""
    remote = request.META.get("REMOTE_ADDR", "")
    trusted = settings.PULSE_TRUSTED_PROXIES
    forwarded = [p.strip() for p in request.META.get("HTTP_X_FORWARDED_FOR", "").split(",") if p.strip()]
    if trusted and len(forwarded) >= trusted:
        return forwarded[-trusted]
    return remote
