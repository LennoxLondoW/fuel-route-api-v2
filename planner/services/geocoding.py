"""
Turn a user-entered place into coordinates.

Resolution order (the first that works wins):
  1. "lat,lon"            e.g. "40.71,-74.00"         -> used as-is, no lookup
  2. "City, ST"/"City, State" e.g. "Austin, TX"       -> City table in PostgreSQL (no network)
  3. anything else        e.g. "1600 Pennsylvania Ave" -> Nominatim (OpenStreetMap) web service

Results are cached, so repeated queries cost nothing. A cache outage only costs speed: the
city table is in PostgreSQL and Nominatim can always be asked again.
"""
 
import hashlib
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from django.conf import settings

from ..exceptions import LocationNotFound
from ..models import City
from . import safe_cache
from .http import get_json
from .states import STATE_CODES, STATES

# Rough bounding box of the USA incl. Alaska, Hawaii and Puerto Rico.
US_LAT = (17.0, 72.0)
US_LON = (-180.0, -64.0)

COORDS_RE = re.compile(r"^\s*(-?\d{1,3}(?:\.\d+)?)\s*,\s*(-?\d{1,3}(?:\.\d+)?)\s*$")
COUNTRY_SUFFIX_RE = re.compile(r",?\s*(usa|us|united states( of america)?)\s*$", re.IGNORECASE)

# Nominatim's public server allows one request per second. When the shared lock in Redis is
# unavailable we fall back to spacing calls out within this process, which is better than
# dropping the limit entirely.
NOMINATIM_MIN_INTERVAL = 1.0
_nominatim_lock = threading.Lock()
_nominatim_last_call = 0.0


def normalize(text):
    """'St. Louis, MO' -> 'st louis mo'. Must match how City names were stored by load_data."""
    s = text.lower().replace(".", "")
    s = re.sub(r"[^a-z ]", " ", s)
    s = re.sub(r"\bsaint\b", "st", s)
    s = re.sub(r"\bmount\b", "mt", s)
    s = re.sub(r"\bfort\b", "ft", s)
    return re.sub(r"\s+", " ", s).strip()


def in_usa(lat, lon):
    """True if the point is inside the rough US bounding box."""
    return US_LAT[0] <= lat <= US_LAT[1] and US_LON[0] <= lon <= US_LON[1]


NOT_FOUND = "NOT_FOUND"


def geocode(query):
    """Return {"query", "name", "lat", "lon", "source"} or raise LocationNotFound."""
    query = query.strip()
    result, cache_key = resolve_local(query)
    if result is None:
        result = _lookup_nominatim(query)
        remember(cache_key, result)
    return _finish(query, result)


def geocode_many(queries):
    """
    Resolve several places at once, in the order given.

    Everything that can be answered locally (coordinates, the cache, the city table) is done
    here on the calling thread, because it reads the database and a worker thread would open
    its own connection outside the caller's transaction. Only the Nominatim lookups overlap,
    and those touch nothing but the network.
    """
    stripped = [q.strip() for q in queries]
    resolved = [resolve_local(q) for q in stripped]
    pending = [i for i, (result, _) in enumerate(resolved) if result is None]

    remote = {}
    if len(pending) > 1:
        with ThreadPoolExecutor(max_workers=len(pending), thread_name_prefix="geocode") as pool:
            # map() yields in order, so an upstream failure surfaces predictably.
            answers = pool.map(_lookup_nominatim, [stripped[i] for i in pending])
            remote = dict(zip(pending, answers))
    elif pending:
        remote = {pending[0]: _lookup_nominatim(stripped[pending[0]])}

    out = []
    for i, (result, cache_key) in enumerate(resolved):
        if result is None:
            result = remote[i]
            remember(cache_key, result)
        out.append(_finish(stripped[i], result))
    return out


def resolve_local(query):
    """
    Everything resolvable without a network call.

    Returns (result, cache_key). `result` is a location dict, the NOT_FOUND sentinel, or None
    when a Nominatim lookup is still needed. `cache_key` is None when there is nothing to
    store, i.e. for coordinates and for answers that already came from the cache.
    """
    # 1) Explicit coordinates.
    m = COORDS_RE.match(query)
    if m:
        lat, lon = float(m.group(1)), float(m.group(2))
        if not in_usa(lat, lon):
            raise LocationNotFound(f'Coordinates "{query}" are outside the USA.')
        return _result(query, query, lat, lon, "coordinates"), None

    # Cache key: hash of the text with case and runs of whitespace folded, so "Austin,  TX"
    # and "austin, tx" share an entry. Nothing else is folded: digits and punctuation
    # distinguish one street address from another.
    folded = " ".join(query.lower().split())
    cache_key = "geocode:" + hashlib.sha256(folded.encode()).hexdigest()
    cached = safe_cache.get(cache_key)
    if cached is not None:
        return cached, None

    # 2) Offline city table. 3) Nominatim is the caller's job, so it can be overlapped.
    city = _lookup_city(query)
    return (city, cache_key) if city else (None, cache_key)


def remember(cache_key, result):
    """Cache a Nominatim answer, including a miss (briefly), so it is not looked up twice."""
    if cache_key is None:
        return
    if result is None:
        safe_cache.set(cache_key, NOT_FOUND, 60 * 60)
    else:
        safe_cache.set(cache_key, result, settings.FUELROUTE["GEOCODE_CACHE_SECONDS"])


def _finish(query, result):
    """Turn a resolved value into the caller's answer, or raise LocationNotFound."""
    if result is None or result == NOT_FOUND:
        raise LocationNotFound(f'Could not find a US location for "{query}".')
    return {**result, "query": query}


def _result(query, name, lat, lon, source):
    """The location dict returned to callers (and included in the API response)."""
    return {"query": query, "name": name, "lat": lat, "lon": lon, "source": source}


def _lookup_city(query):
    """Try to read the input as "<city> <state>", where state is a code ("TX") or a name ("Texas")."""
    words = normalize(COUNTRY_SUFFIX_RE.sub("", query)).split(" ")

    # Try a two-word state name first ("new york"), then a single word ("tx" / "texas").
    for n in (2, 1):
        if len(words) <= n:
            continue
        state_text = " ".join(words[-n:])
        code = state_text if (n == 1 and state_text in STATE_CODES) else STATES.get(state_text)
        if not code:
            continue
        city_name = " ".join(words[:-n])
        row = City.objects.filter(name=city_name, state=code).values_list("lat", "lon").first()
        if row:
            pretty = f"{city_name.title()}, {code.upper()}"
            return _result(query, pretty, row[0], row[1], "city_table")
    return None


def _lookup_nominatim(query):
    """Fallback for street addresses, ZIP codes, and towns missing from the city table."""
    cfg = settings.FUELROUTE
    _wait_for_nominatim_slot()
    _, data = get_json(
        f"{cfg['NOMINATIM_URL']}/search",
        params={"q": query, "format": "jsonv2", "limit": 1, "countrycodes": "us"},
        headers={"User-Agent": cfg["NOMINATIM_USER_AGENT"]},
    )
    if not isinstance(data, list) or not data:
        return None
    try:
        lat, lon = float(data[0]["lat"]), float(data[0]["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not in_usa(lat, lon):
        return None
    name = str(data[0].get("display_name", query))[:200]
    return _result(query, name, lat, lon, "nominatim")


def _wait_for_nominatim_slot():
    """
    Hold off until we are allowed to call Nominatim (max 1 request/second).

    cache.add() is atomic in Redis, so it acts as a lock shared by every server process.
    With the cache unreachable there is nothing to share, so we space calls out within this
    process instead rather than removing the limit.
    """
    for _ in range(5):
        if safe_cache.add("nominatim:slot", 1, timeout=1):
            return
        if safe_cache.cache_degraded():
            _wait_locally()
            return
        time.sleep(0.25)


def _wait_locally():
    """Process-local fallback: keep at least NOMINATIM_MIN_INTERVAL between calls."""
    global _nominatim_last_call
    with _nominatim_lock:
        gap = time.monotonic() - _nominatim_last_call
        if gap < NOMINATIM_MIN_INTERVAL:
            time.sleep(NOMINATIM_MIN_INTERVAL - gap)
        _nominatim_last_call = time.monotonic()
