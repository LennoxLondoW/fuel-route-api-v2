"""
API-key authentication.

Clients send:   X-API-Key: fr_xxxxxxxx...

Failed attempts are counted per client IP; after too many, further *invalid* keys from that
IP are refused for a while, so keys cannot be brute-forced. A valid key always works: the
counter gates guessing, not the legitimate client who happens to sit behind the same NAT
address or proxy as someone else.
"""

import hmac
import logging
from datetime import timedelta

from django.conf import settings
from django.utils import timezone
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.throttling import BaseThrottle

from .exceptions import TooManyAuthFailures
from .models import APIKey
from .services import safe_cache

logger = logging.getLogger(__name__)

HEADER = "HTTP_X_API_KEY"         # Django's name for the "X-API-Key" header
FAILURE_WINDOW_SECONDS = 10 * 60  # failures are counted over a 10-minute window
MAX_KEY_LENGTH = 100              # real keys are ~46 chars; ignore anything absurd


class APIKeyUser:
    """
    Minimal stand-in for a Django user, so DRF's IsAuthenticated works.
    request.user is an APIKeyUser, request.auth is the APIKey row.
    """

    is_authenticated = True
    is_anonymous = False

    def __init__(self, api_key):
        """Wrap the APIKey row that authenticated this request."""
        self.api_key = api_key
        self.pk = api_key.pk

    def __str__(self):
        """Label used in logs, e.g. "api-key:fr_AbC123x"."""
        return f"api-key:{self.api_key.prefix}"


def is_demo_key(raw_key):
    """True if raw_key equals DEMO_API_KEY from .env (constant-time comparison)."""
    demo_key = settings.FUELROUTE["DEMO_API_KEY"]
    return bool(demo_key) and hmac.compare_digest(raw_key.encode(), demo_key.encode())


def register_demo_key(raw_key):
    """
    Save the .env demo key as a normal APIKey row (hash only) the first time it is used,
    so rate limits, last_used_at and deactivation in the admin all apply to it.
    """
    api_key, _ = APIKey.objects.get_or_create(
        hashed_key=APIKey.hash(raw_key),
        defaults={"name": "Demo key (from .env)", "prefix": raw_key[:10]},
    )
    return api_key


class APIKeyAuthentication(BaseAuthentication):
    """DRF authentication class: validates the X-API-Key header against the APIKey table."""

    def authenticate(self, request):
        """
        Return (user, api_key) for a valid key, None if no key was sent,
        or raise AuthenticationFailed / TooManyAuthFailures.

        The key is checked before the brute-force counter is consulted, so a valid key is
        never refused because of someone else's failures from the same address.
        """
        raw_key = request.META.get(HEADER, "").strip()
        if not raw_key:
            return None  # no key -> IsAuthenticated will answer 401

        api_key = None
        if len(raw_key) <= MAX_KEY_LENGTH:
            # Look up by hash: the DB never sees or stores the raw key.
            api_key = APIKey.objects.filter(hashed_key=APIKey.hash(raw_key)).first()
            if api_key is None and is_demo_key(raw_key):
                api_key = register_demo_key(raw_key)

        if api_key is not None and api_key.is_active and not api_key.is_expired:
            self._touch(api_key)
            return APIKeyUser(api_key), api_key

        # Invalid key. Count it, and once this IP is over the limit stop answering at all.
        # Same message for unknown/inactive/expired keys: don't tell attackers which.
        ip = BaseThrottle().get_ident(request)   # honours NUM_PROXIES, like DRF's throttles
        failures = safe_cache.incr_with_window(f"auth_failures:{ip}", FAILURE_WINDOW_SECONDS)
        logger.warning("Rejected API key from %s (failures=%s)", ip, failures)
        if failures is not None and failures >= settings.FUELROUTE["MAX_AUTH_FAILURES"]:
            raise TooManyAuthFailures()
        raise AuthenticationFailed("Invalid or expired API key.")

    def authenticate_header(self, request):
        """Value of the WWW-Authenticate header; returning one makes DRF answer 401 (not 403)."""
        return 'Api-Key realm="api"'

    @staticmethod
    def _touch(api_key):
        """Update the key's last_used_at (shown in the admin, useful to spot unused keys)."""
        # Record usage at most once a minute, so we don't write to the DB on every request.
        now = timezone.now()
        if api_key.last_used_at is None or now - api_key.last_used_at > timedelta(minutes=1):
            APIKey.objects.filter(pk=api_key.pk).update(last_used_at=now)
