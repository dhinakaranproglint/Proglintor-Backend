from django.apps import AppConfig
from django.core.checks import Error, Warning as CheckWarning, register


class PulseConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'pulse'

    def ready(self):
        register(check_cache)


def check_cache(app_configs, **kwargs):
    """Rate limits and the lookup cache rely on the cache: it must work, and in production it must be shared between workers."""
    from django.conf import settings
    from django.core.cache import cache

    problems = []
    try:
        cache.set("pulse:check", 1, 5)
        if cache.get("pulse:check") != 1:
            raise RuntimeError("the cache did not return what was stored")
    except Exception as err:  # noqa: BLE001
        problems.append(Error(f"The cache is not working ({type(err).__name__}). Rate limiting would be skipped.",
                              hint="With CACHE_BACKEND=db run `python manage.py createcachetable` once.", id="pulse.E001"))
    if not settings.DEBUG and not settings.RUNNING_TESTS and settings.CACHES["default"]["BACKEND"].endswith("LocMemCache"):
        problems.append(CheckWarning("The cache is per-process memory, so rate limits are counted separately by every worker.",
                                     hint="Set CACHE_BACKEND=db (or redis) in production.", id="pulse.W001"))
    return problems
