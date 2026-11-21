"""Django settings for the Proglintor backend.

Configuration comes from environment variables (or a `.env` file next to manage.py).
See `.env.example` for every option.
"""
import os
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=VALUE lines); real environment variables win."""
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")


def env(key, default=None):
    return os.environ.get(key, default)


def env_bool(key, default=False):
    return str(env(key, str(default))).lower() in ("1", "true", "yes", "on")


def env_list(key, default=""):
    return [v.strip() for v in env(key, default).split(",") if v.strip()]


DEBUG = env_bool("DJANGO_DEBUG", False)
SECRET_KEY = env("DJANGO_SECRET_KEY") or ("dev-insecure-key-change-me" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("Set DJANGO_SECRET_KEY (or DJANGO_DEBUG=1 for local development).")

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost")
CSRF_TRUSTED_ORIGINS = env_list("DJANGO_CSRF_TRUSTED_ORIGINS", "")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework.authtoken",
    "pulse",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("POSTGRES_DB", "proglint_pulse"),
        "USER": env("POSTGRES_USER", "proglint"),
        "PASSWORD": env("POSTGRES_PASSWORD", ""),
        "HOST": env("POSTGRES_HOST", "127.0.0.1"),
        "PORT": env("POSTGRES_PORT", "5432"),
        "CONN_MAX_AGE": int(env("POSTGRES_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
# Day boundaries for "today" on the admin dashboard use this zone.
TIME_ZONE = env("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Agents upload a whole day of usage plus installed-app lists.
DATA_UPLOAD_MAX_MEMORY_SIZE = 8 * 1024 * 1024

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.TokenAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAdminUser"],
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "UNAUTHENTICATED_USER": None,
}

# Behind an HTTPS reverse proxy (nginx, a load balancer).
BEHIND_PROXY = env_bool("DJANGO_BEHIND_PROXY", False)
# How many proxies append to X-Forwarded-For before a request reaches us (0 = ignore the header: nobody in front of us).
PULSE_TRUSTED_PROXIES = int(env("PULSE_TRUSTED_PROXIES", "1" if BEHIND_PROXY else "0"))

# Rate limits and cached lookups need a cache that every gunicorn worker (and every server) shares.
#   CACHE_BACKEND=db      PostgreSQL table (run `python manage.py createcachetable` once; the Docker image does it). Default in production.
#   CACHE_BACKEND=redis   Redis via CACHE_REDIS_URL (needs the `redis` package).
#   CACHE_BACKEND=locmem  per-process memory: fine for development and tests, wrong for production (a system check warns).
RUNNING_TESTS = len(sys.argv) > 1 and sys.argv[1] == "test"
CACHE_BACKEND = env("CACHE_BACKEND", "locmem" if (DEBUG or RUNNING_TESTS) else "db").lower()
if CACHE_BACKEND == "db":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "pulse_cache"}}
elif CACHE_BACKEND == "redis":
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": env("CACHE_REDIS_URL", "redis://127.0.0.1:6379/1")}}
else:
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}

# Behind an HTTPS reverse proxy: believe its X-Forwarded-Proto, and make cookies and links HTTPS-only.
if BEHIND_PROXY:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    USE_X_FORWARDED_HOST = True
    if not DEBUG:
        SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
        SESSION_COOKIE_SECURE = True
        CSRF_COOKIE_SECURE = True
        SECURE_HSTS_SECONDS = int(env("SECURE_HSTS_SECONDS", str(30 * 24 * 3600)))  # 30 days; raise it once HTTPS is proven everywhere
        SECURE_CONTENT_TYPE_NOSNIFF = True

# A production deployment must not carry development settings. Set PULSE_ENV=production on the server and the app refuses to start otherwise.
if env("PULSE_ENV", "").lower() == "production":
    if DEBUG or "*" in ALLOWED_HOSTS or SECRET_KEY.startswith("dev-insecure"):
        raise RuntimeError("PULSE_ENV=production needs DJANGO_DEBUG=0, explicit DJANGO_ALLOWED_HOSTS (no '*') and a real DJANGO_SECRET_KEY.")

# E-mail (password reset codes). In development the e-mail is printed in the server console.
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.console.EmailBackend" if DEBUG else "django.core.mail.backends.smtp.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "587"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env_bool("EMAIL_USE_TLS", True)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "Proglintor <no-reply@proglint.com>")
PASSWORD_RESET_MINUTES = int(env("PASSWORD_RESET_MINUTES", "15"))

# Agents are considered online if they reported within this many seconds.
PULSE_ONLINE_SECONDS = int(env("PULSE_ONLINE_SECONDS", "90"))
# Software install requests: limits and timeouts.
SOFTWARE_MAX_OPEN_PER_ASSET = int(env("SOFTWARE_MAX_OPEN_PER_ASSET", "5"))
SOFTWARE_SUBMITS_PER_10MIN = int(env("SOFTWARE_SUBMITS_PER_10MIN", "20"))
SOFTWARE_APPROVAL_TTL_HOURS = int(env("SOFTWARE_APPROVAL_TTL_HOURS", "168"))  # unreviewed requests expire after a week
SOFTWARE_QUEUE_TTL_HOURS = int(env("SOFTWARE_QUEUE_TTL_HOURS", "24"))  # approved, but the laptop never picked it up
SOFTWARE_STALE_MINUTES = int(env("SOFTWARE_STALE_MINUTES", "15"))
SOFTWARE_INVENTORY_MAX_AGE_MINUTES = int(env("SOFTWARE_INVENTORY_MAX_AGE_MINUTES", "10"))  # an older installed-apps list is not trusted for "already installed"  # install with no progress report = interrupted
# Conversational assistant (Google Gemini). Optional: without a key the chat uses its built-in replies.
# Switches for the AI features (both default on when a key is set). Turn off what your privacy review does not allow:
#   ASSISTANT_CHAT_ENABLED=0         no message is ever sent to the model; built-in replies only (this also disables the web lookup below).
#   ASSISTANT_WEB_LOOKUP_ENABLED=0   keep the chat, but never look up software that isn't in the catalogue.
ASSISTANT_CHAT_ENABLED = env_bool("ASSISTANT_CHAT_ENABLED", True)
ASSISTANT_WEB_LOOKUP_ENABLED = env_bool("ASSISTANT_WEB_LOOKUP_ENABLED", True)
GEMINI_API_KEY = env("GEMINI_API_KEY", "")
GEMINI_MODEL = env("GEMINI_MODEL", "gemini-2.5-flash-lite")
GEMINI_TIMEOUT_SECONDS = min(int(env("GEMINI_TIMEOUT_SECONDS", "12")), 12)  # a slow model must not stall the chat
GEMINI_MAX_OUTPUT_TOKENS = int(env("GEMINI_MAX_OUTPUT_TOKENS", "400"))
ASSIST_PER_10MIN = int(env("ASSIST_PER_10MIN", "30"))  # model calls per laptop
PROPOSALS_PER_DAY = int(env("PROPOSALS_PER_DAY", "10"))  # "find this app for me" web lookups per laptop
# Address shown to admins for connecting agents (defaults to the URL the admin app uses).
PULSE_PUBLIC_URL = env("PULSE_PUBLIC_URL", "")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "root": {"handlers": ["console"], "level": env("DJANGO_LOG_LEVEL", "INFO")},
}
