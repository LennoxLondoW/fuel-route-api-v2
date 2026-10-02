"""
Django settings for the Fuel Route API.

Every secret / environment-specific value is read from environment variables,
which python-dotenv loads from the ".env" file next to manage.py.
See ".env.example" for the full list.
"""

import os
import sys
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# Load ".env" into os.environ (real environment variables still win).
load_dotenv(BASE_DIR / ".env")


def env(name, default=None, required=False):
    """Read an environment variable; fail loudly if a required one is missing."""
    value = os.environ.get(name, default)
    if required and not value:
        raise ImproperlyConfigured(f"Environment variable {name} is required.")
    return value


def env_bool(name, default=False):
    """Read a true/false environment variable ("1", "true", "yes", "on" mean True)."""
    return str(env(name, str(default))).strip().lower() in ("1", "true", "yes", "on")


def env_list(name, default=""):
    """Read a comma-separated environment variable as a list of strings."""
    return [item.strip() for item in env(name, default).split(",") if item.strip()]


# True while running "manage.py test" (used to swap Redis for an in-memory cache, etc.)
TESTING = len(sys.argv) > 1 and sys.argv[1] == "test"


# ---------------------------------------------------------------------------
# Core security
# ---------------------------------------------------------------------------

SECRET_KEY = env("DJANGO_SECRET_KEY", required=True)

# Off unless explicitly turned on: never ship DEBUG=True to production.
DEBUG = env_bool("DJANGO_DEBUG", False)

ALLOWED_HOSTS = env_list("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1")


# ---------------------------------------------------------------------------
# Applications
# ---------------------------------------------------------------------------

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Third party
    "rest_framework",
    "corsheaders",
    # Local
    "planner",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    # Adds an X-Request-ID to every request/response and strict headers on API responses.
    "planner.middleware.RequestIDMiddleware",
    # CORS must run before anything that can return a response (e.g. CommonMiddleware).
    "corsheaders.middleware.CorsMiddleware",
    # Route geometry can be large; gzip shrinks it ~3-4x.
    "django.middleware.gzip.GZipMiddleware",
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


# ---------------------------------------------------------------------------
# Database: PostgreSQL
# ---------------------------------------------------------------------------

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": env("DB_NAME", "fuelroute"),
        "USER": env("DB_USER", "postgres"),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", "localhost"),
        "PORT": env("DB_PORT", "5432"),
        # Reuse connections for 60s instead of reconnecting on every request.
        "CONN_MAX_AGE": 60,
        "CONN_HEALTH_CHECKS": True,
        "OPTIONS": {"connect_timeout": 5},
    }
}

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# ---------------------------------------------------------------------------
# Cache: Redis (also stores the rate-limit counters)
# ---------------------------------------------------------------------------

if TESTING:
    # Tests must not depend on (or pollute) a real Redis server.
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}}
else:
    CACHES = {
        "default": {
            "BACKEND": "django.core.cache.backends.redis.RedisCache",
            # Use 127.0.0.1, not "localhost": on Windows "localhost" tries IPv6 first,
            # which adds ~2 s to every new connection when Redis only listens on IPv4.
            "LOCATION": env("REDIS_URL", "redis://127.0.0.1:6379/0"),
            "KEY_PREFIX": "fuelroute",
            "TIMEOUT": 60 * 60,  # default TTL: 1 hour
            # Fail fast if Redis is unreachable instead of hanging the request.
            "OPTIONS": {"socket_connect_timeout": 2, "socket_timeout": 2},
        }
    }


# ---------------------------------------------------------------------------
# Django REST Framework
# ---------------------------------------------------------------------------

REST_FRAMEWORK = {
    # Every endpoint requires a valid API key unless a view explicitly opts out.
    "DEFAULT_AUTHENTICATION_CLASSES": ["planner.authentication.APIKeyAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    # JSON in, JSON out. No browsable HTML API, no form/multipart parsing.
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["rest_framework.parsers.JSONParser"],
    # Rate limits per API key: a sustained limit plus a short burst limit.
    "DEFAULT_THROTTLE_CLASSES": [
        "planner.throttling.APIKeyBurstThrottle",
        "planner.throttling.APIKeySustainedThrottle",
    ],
    "DEFAULT_THROTTLE_RATES": {
        "burst": env("THROTTLE_RATE_BURST", "5/second"),
        "sustained": env("THROTTLE_RATE_ROUTE", "30/minute"),
        # The health check gets its own generous budget: a fleet of probes behind one proxy
        # address must not throttle itself into looking unhealthy.
        "health": env("THROTTLE_RATE_HEALTH", "120/minute"),
    },
    # One consistent error format for every failure.
    "EXCEPTION_HANDLER": "planner.handlers.api_exception_handler",
    # request.user is None (not AnonymousUser) when no key was sent.
    "UNAUTHENTICATED_USER": None,
}


# ---------------------------------------------------------------------------
# CORS: only these web origins may call the API from a browser
# ---------------------------------------------------------------------------

CORS_ALLOWED_ORIGINS = env_list("CORS_ALLOWED_ORIGINS", "")
CORS_URLS_REGEX = r"^/api/.*$"          # CORS headers only on API routes
CORS_ALLOW_METHODS = ["GET", "POST", "OPTIONS"]
CORS_ALLOW_HEADERS = ["accept", "content-type", "x-api-key", "x-request-id"]
CORS_EXPOSE_HEADERS = ["x-request-id", "retry-after"]
CORS_ALLOW_CREDENTIALS = False          # API keys, not cookies
CORS_PREFLIGHT_MAX_AGE = 86400


# ---------------------------------------------------------------------------
# HTTP hardening
# ---------------------------------------------------------------------------

# Reject oversized request bodies early (the API only takes two short strings).
DATA_UPLOAD_MAX_MEMORY_SIZE = 10 * 1024          # 10 KB
DATA_UPLOAD_MAX_NUMBER_FIELDS = 50

SECURE_CONTENT_TYPE_NOSNIFF = True
# "same-origin" strips the Referer entirely on cross-origin requests, which left the map tiles
# with nothing identifying the app. OpenStreetMap's tile servers require a Referer or
# User-Agent that names the application and answer 403 "App is not following the tile usage
# policy" without one. This policy sends only the scheme and host cross-origin, never the path
# or query string, so a user's typed locations still never leave the server.
SECURE_REFERRER_POLICY = "strict-origin-when-cross-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
X_FRAME_OPTIONS = "DENY"
SESSION_COOKIE_HTTPONLY = True

if not DEBUG and not TESTING:
    # Production: force HTTPS everywhere.
    SECURE_SSL_REDIRECT = env_bool("SECURE_SSL_REDIRECT", True)
    # Trust X-Forwarded-Proto from the reverse proxy (nginx, load balancer) that terminates TLS.
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True


# ---------------------------------------------------------------------------
# Password validation (only used for Django admin accounts)
# ---------------------------------------------------------------------------

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ---------------------------------------------------------------------------
# Internationalization / static files
# ---------------------------------------------------------------------------

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


# ---------------------------------------------------------------------------
# Logging: everything to the console, so Docker/systemd can collect it
# ---------------------------------------------------------------------------

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name} {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
    "loggers": {
        # Keep test output quiet.
        "planner": {"level": "CRITICAL" if TESTING else env("LOG_LEVEL", "INFO")},
        # Django logs every 4xx as a warning; tests trigger many on purpose.
        "django.request": {"level": "ERROR" if TESTING else "WARNING"},
    },
}


# ---------------------------------------------------------------------------
# Fuel-route settings
# ---------------------------------------------------------------------------

FUELROUTE = {
    # Vehicle assumptions (same as the original HTML prototype).
    "RANGE_MILES": 500,          # max distance on a full tank
    "MPG": 10,                   # miles per gallon
    # Stations up to this far from the route are considered. The price file gives city-level
    # coordinates, not pump locations, so a tight corridor is false precision; at 10 miles
    # sparse western routes left gaps of up to 469 miles against a 500-mile range, which is
    # uncomfortably close to rejecting a perfectly drivable trip. Detours are charged for, so
    # a wider corridor does not make far-off stations look free.
    "CORRIDOR_MILES": 20,
    "MIN_SAVING_PER_GAL": 0.03,  # only detour to a cheaper station if it saves at least this much per gallon
    "MIN_HOP_MILES": 100,        # prefer stops at least this far apart
    # Range still in the tank on arriving anywhere, held back from RANGE_MILES.
    #
    # Zero by default, because the brief states a 500-mile maximum range and that is what the
    # planner must be willing to use. A non-zero reserve plans every leg to finish with fuel to
    # spare, which is how a real driver behaves given that station positions here are only
    # city-accurate, but it also means refusing a 490-mile gap the vehicle could in fact clear.
    # Raise it only if you would rather the planner be cautious than literal.
    "RESERVE_MILES": 0,

    # Upstream services (server-side only; the user never controls these URLs).
    "OSRM_URL": env("OSRM_URL", "https://router.project-osrm.org").rstrip("/"),
    "NOMINATIM_URL": env("NOMINATIM_URL", "https://nominatim.openstreetmap.org").rstrip("/"),
    "NOMINATIM_USER_AGENT": env("NOMINATIM_USER_AGENT", "fuelroute-api/1.0"),
    "HTTP_TIMEOUT_SECONDS": 10,
    "MAX_UPSTREAM_BYTES": 10 * 1024 * 1024,   # never read more than 10 MB from an upstream

    # How long to cache upstream answers in Redis.
    "GEOCODE_CACHE_SECONDS": 60 * 60 * 24 * 7,   # 7 days
    "ROUTE_CACHE_SECONDS": 60 * 60 * 24,         # 1 day

    # Brute-force protection: failed API-key attempts allowed per IP per 10 minutes.
    "MAX_AUTH_FAILURES": 20,

    # Optional API key from .env: accepted by the API and pre-filled on the demo page.
    # Leave empty to disable (keys then come only from create_api_key / the admin).
    "DEMO_API_KEY": env("DEMO_API_KEY", "").strip(),
}

# A guessable demo key would defeat the API-key protection.
if FUELROUTE["DEMO_API_KEY"] and len(FUELROUTE["DEMO_API_KEY"]) < 32:
    raise ImproperlyConfigured("DEMO_API_KEY must be at least 32 characters (or empty to disable).")
