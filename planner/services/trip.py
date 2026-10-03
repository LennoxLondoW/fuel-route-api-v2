"""
The full pipeline for one request, and the JSON shape returned to the client.
"""

import time
from decimal import ROUND_HALF_UP, Decimal

from django.conf import settings

from . import safe_cache
from .fuel_plan import plan_fuel_stops
from .geocoding import geocode_many
from .routing import decode_polyline, get_route
from .stations import get_station_index

TENTH_CENT = Decimal("0.001")


def plan_trip(origin, destination, geometry="polyline"):
    """
    origin/destination: free text ("New York, NY", "40.7,-74.0", an address...)
    geometry: "polyline" -> route as an encoded polyline string (default; ~6x smaller)
              "points"   -> route as [[lat, lon], ...]  (easiest to plot directly)
    """
    cfg = settings.FUELROUTE
    timings = {}
    t = time.perf_counter()

    # 1) Where are we going? Local lookups run inline; any Nominatim calls overlap.
    start, end = geocode_many([origin, destination])
    timings["geocode"] = _ms_since(t)

    # 2) Which roads? (one OSRM call, or zero if cached)
    t = time.perf_counter()
    route = get_route(start, end)
    timings["route"] = _ms_since(t)

    # 3) Which stations are on the way, and where should we stop?
    # The route arrives already thinned with real road distances, so nothing is decoded here
    # unless the caller asked for the full point list.
    t = time.perf_counter()
    miles = route["distance_miles"]
    index = get_station_index()
    candidates = index.along_route(
        route["route_lat"], route["route_lon"], route["route_cum"], cfg["CORRIDOR_MILES"])
    plan = plan_fuel_stops(
        candidates, miles,
        range_miles=cfg["RANGE_MILES"], mpg=cfg["MPG"],
        min_saving=cfg["MIN_SAVING_PER_GAL"], min_hop=cfg["MIN_HOP_MILES"],
        reserve_miles=cfg["RESERVE_MILES"],
        fallback_price=index.average_price,
    )
    timings["plan"] = _ms_since(t)

    stops = [_stop_json(i, s, cfg["MPG"]) for i, s in enumerate(plan.stops, start=1)]

    return {
        "from": start,
        "to": end,
        "summary": {
            "distance_miles": round(miles, 1),
            "duration_seconds": round(route["duration_seconds"]),
            "fuel_used_gallons": round(miles / cfg["MPG"], 2),
            "fuel_stops": len(stops),
            "total_cost_usd": plan.total_cost,
            "starting_tank": {
                "gallons": round(plan.start_gallons, 2),
                "price_per_gallon": _price(plan.start_price),
                "price_source": plan.start_price_source,
                "cost_usd": plan.start_cost,
            },
            "fuel_bought_on_route_usd": sum((s.cost for s in plan.stops), Decimal("0.00")),
            "leftover_fuel_gallons": round(plan.leftover_gallons, 2),
            "leftover_fuel_credit_usd": plan.leftover_credit,
        },
        "stops": stops,
        # Everything to put on a map, in drawing order.
        "markers": (
            [_marker("start", start["lat"], start["lon"], start["name"])]
            + [_marker("fuel_stop", s["lat"], s["lon"], f'{s["sequence"]}. {s["name"]}', s["sequence"])
               for s in stops]
            + [_marker("finish", end["lat"], end["lon"], end["name"])]
        ),
        "route": _route_json(route, geometry),
        "assumptions": {
            "vehicle_range_miles": cfg["RANGE_MILES"],
            "miles_per_gallon": cfg["MPG"],
            "station_corridor_miles": cfg["CORRIDOR_MILES"],
            "reserve_miles": cfg["RESERVE_MILES"],
            "starts_with_full_tank": True,
            "stations_are_on_route": True,
            "station_positions": "city-level (the price list has no exact coordinates)",
        },
        "meta": {
            "route_cached": route["cached"],
            "stations_considered": len(candidates),
            # "osrm" = true road distance per vertex; "estimated" = straight lines rescaled
            # to the trip total, which spreads the error unevenly along the route.
            "distance_source": "osrm" if route.get("exact_distances") else "estimated",
            # True if the cache was unreachable recently: answers are still correct, but
            # slower, and rate limiting is not being counted.
            "cache_degraded": safe_cache.cache_degraded(),
            "timings_ms": timings,
        },
    }


def _stop_json(sequence, stop, mpg):
    """One fuel stop as JSON: where it is, the price, and how much to buy."""
    st = stop.station
    return {
        "sequence": sequence,
        "station_id": st.id,
        "name": st.name,
        "city": st.city,
        "state": st.state,
        "lat": st.lat,
        "lon": st.lon,
        "mile": round(st.mile, 1),
        # How far the station's city centre is from the route line. Reported as information;
        # the price file has no pump coordinates, and these are highway truck stops, so it is
        # not treated as a detour to be driven.
        "distance_from_route_miles": round(st.offset, 1),
        "price_per_gallon": _price(st.price),
        "fuel_on_arrival_gallons": round(stop.arrive_miles / mpg, 2),
        "gallons": round(stop.gallons, 2),
        "cost_usd": stop.cost,
    }


def _marker(kind, lat, lon, label, sequence=None):
    """A map pin: type is "start", "fuel_stop" or "finish"."""
    marker = {"type": kind, "lat": lat, "lon": lon, "label": label}
    if sequence is not None:
        marker["sequence"] = sequence
    return marker


def _route_json(route, geometry):
    """
    The route line, as the compact encoded polyline (default) or as a list of points.

    "points" is what Leaflet takes directly, but it is roughly six times the bytes, and it is
    the only reason this request has to decode the polyline at all.
    """
    out = {
        "format": geometry,
        "point_count": route["point_count"],
        # [[south, west], [north, east]]: pass straight to Leaflet's map.fitBounds().
        "bounds": route["bounds"],
    }
    if geometry == "points":
        lats, lons = decode_polyline(route["polyline"])
        out["points"] = [[la, lo] for la, lo in zip(lats, lons)]
    else:
        out["polyline"] = route["polyline"]
        out["precision"] = 5
    return out


def _price(value):
    """Round a per-gallon price to 1/10 cent, as fuel prices are quoted (None stays None)."""
    return None if value is None else value.quantize(TENTH_CENT, rounding=ROUND_HALF_UP)


def _ms_since(t):
    """Milliseconds elapsed since perf_counter() value t."""
    return round((time.perf_counter() - t) * 1000, 1)
