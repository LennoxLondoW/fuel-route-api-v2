"""
Driving route between two points, from an OSRM server.

OSRM returns the route shape as a Google "encoded polyline" string, about 5x smaller than
a list of coordinates, plus (because we ask for `annotations=distance`) the true road
distance of every segment between consecutive vertices.

Decoding ~35k vertices costs over 100 ms of pure Python, so the cache entry holds not just
the polyline but everything derived from it that the planner needs: the map bounds and the
route thinned to ~1 point per mile with exact cumulative road distances. A cached route
therefore needs no decoding at all unless the caller asked for `geometry=points`.
"""

import logging
import math

from django.conf import settings

from ..exceptions import RouteNotFound, UpstreamUnavailable
from . import safe_cache
from .http import get_json

logger = logging.getLogger(__name__)

METERS_PER_MILE = 1609.344
MILES_PER_DEG_LAT = 69.09
MILES_PER_DEG_LON = 69.17   # at the equator; multiply by cos(latitude)
STEP_MILES = 1.0            # route thinning: keep about one point per mile

# Bump when the shape of a cached route entry changes, so old entries are ignored.
CACHE_SCHEMA = "v2"


def get_route(start, end):
    """
    start/end: dicts with "lat" and "lon".

    Returns a dict with the trip totals ("distance_miles", "duration_seconds"), the encoded
    "polyline" plus its "point_count" and "bounds", the thinned route ("route_lat",
    "route_lon", "route_cum" in miles from the start), whether the route came from the
    cache, and whether its distances are true road distances ("exact_distances").
    """
    # Round to ~10 m so nearly identical requests share a cache entry.
    coords = f"{start['lon']:.4f},{start['lat']:.4f};{end['lon']:.4f},{end['lat']:.4f}"
    cache_key = f"route:{CACHE_SCHEMA}:{coords}"

    cached = safe_cache.get(cache_key)
    if cached is not None:
        return {**cached, "cached": True}

    # 400 is how OSRM reports "no route"/bad input, so it is not an outage.
    status_code, data = get_json(
        f"{settings.FUELROUTE['OSRM_URL']}/route/v1/driving/{coords}",
        params={"overview": "full", "geometries": "polyline", "annotations": "distance"},
        ok_statuses=(200, 400),
    )

    if data.get("code") != "Ok" or not data.get("routes"):
        if data.get("code") in ("NoRoute", "NoSegment"):
            raise RouteNotFound()
        logger.warning("OSRM error: %s %s", status_code, data.get("code"))
        raise UpstreamUnavailable()

    best = data["routes"][0]
    miles = best["distance"] / METERS_PER_MILE
    polyline = best["geometry"]

    lats, lons = decode_polyline(polyline)
    if not lats:
        raise UpstreamUnavailable()

    segment_miles = _segment_miles(best, len(lats))
    pts_lat, pts_lon, cum = thin_route(lats, lons, miles, segment_miles)

    route = {
        "distance_miles": miles,
        "duration_seconds": best["duration"],
        "polyline": polyline,
        "point_count": len(lats),
        # [[south, west], [north, east]]: pass straight to Leaflet's map.fitBounds().
        "bounds": [[min(lats), min(lons)], [max(lats), max(lons)]],
        "route_lat": pts_lat,
        "route_lon": pts_lon,
        "route_cum": cum,
        "exact_distances": segment_miles is not None,
    }
    safe_cache.set(cache_key, route, settings.FUELROUTE["ROUTE_CACHE_SECONDS"])
    return {**route, "cached": False}


def _segment_miles(route, vertex_count):
    """
    Per-segment road distances in miles, from OSRM's `annotations=distance`.

    The annotation arrays run one entry per pair of consecutive geometry vertices, so the
    legs concatenated must come to vertex_count - 1 entries. Returns None if the server
    did not send them or they do not line up, in which case the caller falls back to
    straight-line distances rescaled to the trip total.
    """
    distances = []
    for leg in route.get("legs") or ():
        annotation = (leg or {}).get("annotation") or {}
        leg_distances = annotation.get("distance")
        if not isinstance(leg_distances, list):
            return None
        distances.extend(leg_distances)

    if len(distances) != vertex_count - 1:
        if distances:
            logger.warning("OSRM annotation length %s != %s vertices - 1",
                           len(distances), vertex_count)
        return None
    try:
        return [d / METERS_PER_MILE for d in distances]
    except TypeError:
        return None


def thin_route(lats, lons, route_miles, segment_miles=None):
    """
    Thin the route to roughly one point per mile, and measure distance along it.

    Returns (pts_lat, pts_lon, cum), where cum[i] is the distance in miles from the start
    to the kept point i.

    With `segment_miles` (OSRM's own per-segment road distances) the cumulative figures are
    exact: skipped vertices still contribute their full road length. Without it we fall
    back to summing straight-line chords between kept points and rescaling the whole route
    to match OSRM's total, which is several percent short before rescaling and leaves an
    uneven error wherever the road curves more than average.
    """
    if not lats:
        return [], [], []

    pts_lat, pts_lon, cum = [lats[0]], [lons[0]], [0.0]
    cos_lat = math.cos(math.radians(lats[0]))
    last = len(lats) - 1
    run = 0.0   # road miles accumulated since the last kept point

    for i in range(1, len(lats)):
        if segment_miles is not None:
            run += segment_miles[i - 1]
        # Flat-earth approximation, used only to decide whether to keep this point.
        dy = (lats[i] - pts_lat[-1]) * MILES_PER_DEG_LAT
        dx = (lons[i] - pts_lon[-1]) * MILES_PER_DEG_LON * cos_lat
        d2 = dx * dx + dy * dy
        if d2 >= STEP_MILES * STEP_MILES or i == last:
            pts_lat.append(lats[i])
            pts_lon.append(lons[i])
            cum.append(cum[-1] + (run if segment_miles is not None else math.sqrt(d2)))
            cos_lat = math.cos(math.radians(lats[i]))
            run = 0.0

    if segment_miles is None and cum[-1]:
        # Straight segments fall short of the road distance: rescale to OSRM's total.
        scale = route_miles / cum[-1]
        cum = [c * scale for c in cum]

    return pts_lat, pts_lon, cum


def decode_polyline(encoded, precision=5):
    """
    Decode a Google encoded polyline into two lists: latitudes and longitudes.
    Format: https://developers.google.com/maps/documentation/utilities/polylinealgorithm
    Each coordinate is stored as a difference from the previous one, packed into 5-bit chunks.

    Raises UpstreamUnavailable on a truncated or malformed string, so a bad upstream
    response (or a corrupted cache entry) is reported as an upstream problem, not a crash.
    """
    factor = 10 ** precision
    lats, lons = [], []
    index, lat, lon = 0, 0, 0
    length = len(encoded)

    while index < length:
        deltas = []
        for _ in range(2):  # one latitude delta, then one longitude delta
            result, shift = 0, 0
            while True:
                if index >= length:
                    logger.warning("Truncated polyline after %s points", len(lats))
                    raise UpstreamUnavailable()
                b = ord(encoded[index]) - 63
                index += 1
                if b < 0:
                    logger.warning("Invalid polyline character at offset %s", index - 1)
                    raise UpstreamUnavailable()
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:  # last chunk of this number
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)

        lat += deltas[0]
        lon += deltas[1]
        lats.append(lat / factor)
        lons.append(lon / factor)

    return lats, lons
