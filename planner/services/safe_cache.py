"""
Cache access that degrades instead of failing.

Redis is an accelerator here, not a source of truth: the station index lives in process
memory, cities live in PostgreSQL, and OSRM/Nominatim can always be called again. So a
Redis outage must slow the API down, never take it out.

Every helper below swallows backend errors (connection refused, timeout, serialization
problems) and behaves as though the entry simply was not there. Rate limiting is the one
exception: DRF's throttles talk to the cache directly, so when Redis is gone they stop
counting. That is the documented trade-off, recorded in `cache_degraded()` so callers can
report it.

Two extra jobs:
  - `ping()` tracks whether the backend answered recently, which the health check reports.
  - `get_versioned()` reads a value at most once every CHECK_INTERVAL seconds, so a hot
    path can poll a version marker without a Redis round-trip on every single request.
"""

import logging
import threading
import time

from django.core.cache import cache

logger = logging.getLogger(__name__)

# How long a cached "version" read is reused before asking the backend again.
CHECK_INTERVAL_SECONDS = 5.0

# Remember the last failure so we do not log one line per request during an outage.
_LOG_EVERY_SECONDS = 30.0
_last_logged = 0.0
_last_failure = 0.0
_lock = threading.Lock()


def _note_failure(operation, exc):
    """Record a backend failure and log it at most once every _LOG_EVERY_SECONDS."""
    global _last_logged, _last_failure
    now = time.monotonic()
    with _lock:
        _last_failure = now
        if now - _last_logged < _LOG_EVERY_SECONDS:
            return
        _last_logged = now
    logger.warning("Cache unavailable (%s): %s", operation, exc)


def cache_degraded():
    """True if a cache operation failed in the last minute (reported in meta/health)."""
    return _last_failure > 0.0 and (time.monotonic() - _last_failure) < 60.0


def get(key, default=None):
    """cache.get() that returns `default` when the backend is unreachable."""
    try:
        value = cache.get(key, default)
    except Exception as exc:
        _note_failure("get", exc)
        return default
    return value


def set(key, value, timeout=None):  # noqa: A001 - mirrors the cache API on purpose
    """cache.set() that reports whether the value was actually stored."""
    try:
        cache.set(key, value, timeout)
    except Exception as exc:
        _note_failure("set", exc)
        return False
    return True


def add(key, value, timeout=None):
    """
    cache.add() that returns False when the backend is unreachable.

    Callers use this as a lock, so a failure must read as "someone else holds it"
    rather than "the lock is free": that keeps us from hammering a rate-limited upstream.
    """
    try:
        return bool(cache.add(key, value, timeout))
    except Exception as exc:
        _note_failure("add", exc)
        return False


def incr_with_window(key, window_seconds):
    """
    Increment a counter that expires `window_seconds` after its first increment.

    Returns the new count, or None if the backend is unreachable (the caller then has no
    counter to enforce). add() first, so the expiry window starts at the first increment.
    """
    try:
        cache.add(key, 0, window_seconds)
        try:
            return cache.incr(key)
        except ValueError:
            # The key expired between add() and incr(); start a fresh window.
            cache.set(key, 1, window_seconds)
            return 1
    except Exception as exc:
        _note_failure("incr", exc)
        return None


def ping():
    """Write and read back one key. True if the backend is healthy."""
    try:
        cache.set("health:ping", 1, 5)
        ok = cache.get("health:ping") == 1
    except Exception as exc:
        _note_failure("ping", exc)
        return False
    if not ok:
        _note_failure("ping", "read-back mismatch")
    return ok


# ---------------------------------------------------------------------------
# Throttled reads, for version markers polled on every request.
# ---------------------------------------------------------------------------

_versions = {}   # key -> (value, monotonic time it was read)


def get_versioned(key, default=0):
    """
    Read `key`, reusing the previous answer for up to CHECK_INTERVAL_SECONDS.

    Used for the station-data version marker: a reload becomes visible within a few
    seconds, without spending a Redis round-trip on every request.
    """
    now = time.monotonic()
    cached = _versions.get(key)
    if cached is not None and now - cached[1] < CHECK_INTERVAL_SECONDS:
        return cached[0]

    value = get(key, None)
    if value is None:
        # Unreachable backend, or no marker set yet: keep whatever we last saw, so an
        # outage never looks like a data change and never triggers a pointless rebuild.
        value = cached[0] if cached is not None else default
    _versions[key] = (value, now)
    return value


def reset_versioned(key=None):
    """Forget throttled reads, so the next get_versioned() hits the backend. For tests."""
    if key is None:
        _versions.clear()
    else:
        _versions.pop(key, None)


def reset_for_tests():
    """Clear throttled reads and the recorded-failure state, so tests start clean."""
    global _last_failure, _last_logged
    _versions.clear()
    _last_failure = 0.0
    _last_logged = 0.0
