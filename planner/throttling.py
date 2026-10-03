"""
Rate limits, counted per API key (or per IP for requests without a key).
Counters live in the default cache, i.e. Redis, so they are shared by all server processes.
Rates are configured in settings.REST_FRAMEWORK["DEFAULT_THROTTLE_RATES"].

Note that when the cache is unreachable DRF's throttles stop counting, so limits are not
enforced during an outage. That is deliberate: the alternative is refusing every request.
`services.safe_cache.cache_degraded()` reports it, and the API includes it in `meta`.
"""

from rest_framework.throttling import SimpleRateThrottle

from .models import APIKey
from .services import safe_cache


class _APIKeyThrottle(SimpleRateThrottle):
    """Shared logic: one request counter per API key, falling back to the client IP."""

    def get_cache_key(self, request, view):
        """Name of the Redis key that holds this client's request timestamps for this scope."""
        if isinstance(request.auth, APIKey):
            ident = f"key:{request.auth.pk}"
        else:
            ident = f"ip:{self.get_ident(request)}"
        return f"throttle:{self.scope}:{ident}"

    def allow_request(self, request, view):
        """
        Count this request, and let it through if the cache cannot be reached.

        DRF's throttles read and write the cache directly rather than through
        services.safe_cache, so without this a Redis outage would raise here and turn every
        API request into a 500. Failing open is the deliberate choice: the alternative is
        refusing all traffic because the rate limiter is unavailable. The outage is recorded,
        so `meta.cache_degraded` and the health check both report that limits are not being
        enforced.
        """
        try:
            return super().allow_request(request, view)
        except Exception as exc:
            safe_cache.report_failure(f"throttle:{self.scope}", exc)
            return True


class APIKeyBurstThrottle(_APIKeyThrottle):
    """Short window: stops a client from firing many requests at once."""

    scope = "burst"


class APIKeySustainedThrottle(_APIKeyThrottle):
    """Longer window: caps overall usage."""

    scope = "sustained"


class HealthThrottle(_APIKeyThrottle):
    """
    Generous limit for the public health check.

    Probes from a load balancer all arrive from one address, and several of them share it, so
    the normal per-IP burst limit would hand out 429s that a balancer reads as "unhealthy"
    and act on by pulling a perfectly good instance out of service.
    """

    scope = "health"
