"""
Find the fuel stations that lie along a route.

Brute force (every station x every route point) would be ~6,600 x 30,000 = 200M distance
checks for a coast-to-coast trip. Instead:

  1. All stations are put once into a grid of 0.25° x 0.25° cells (kept in memory).
  2. The route arrives already thinned to ~1 point per mile (see routing.thin_route).
  3. The thinned points are bucketed into the same grid, and only stations sharing a
     neighbourhood with a route point are measured at all.

Scanning stations per route point (the obvious direction) spends most of its time on empty
cells, because a 0.25° cell is ~17 miles across and route points are a mile apart, so the
same cells get rescanned a dozen times. Walking the stations instead and measuring each one
against only the route points near it does the same work once: measured at 12 ms against
24 ms for a coast-to-coast route, with identical output.

Prices stay Decimal all the way through, so money arithmetic never picks up binary
floating-point drift. Positions stay float, which is all the precision coordinates deserve.
"""

import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

from ..models import FuelStation
from . import safe_cache

CELL_DEG = 0.25           # grid cell size in degrees
MILES_PER_DEG_LAT = 69.09
MILES_PER_DEG_LON = 69.17  # at the equator; multiply by cos(latitude)
DATA_VERSION_KEY = "stations:version"


@dataclass
class Candidate:
    """A station near the route, with its position along the route."""

    id: int
    name: str
    city: str
    state: str
    lat: float
    lon: float
    price: Decimal   # USD per gallon; kept exact for the cost arithmetic
    mile: float      # distance from the start, along the route
    offset: float    # distance from the route (straight line), in miles

    def __post_init__(self):
        """Accept a float or string price from callers and store it as an exact Decimal."""
        if not isinstance(self.price, Decimal):
            self.price = Decimal(str(self.price))


class StationIndex:
    """All stations, bucketed by grid cell."""

    def __init__(self, stations):
        """
        Build the grid. stations: list of (id, name, city, state, lat, lon, price) tuples.

        Prices arrive as Decimal from the database; anything else is converted, so a caller
        passing plain numbers gets exact arithmetic rather than a confusing type error deep
        in the cost calculation.
        """
        self.stations = [
            s if isinstance(s[6], Decimal) else (*s[:6], Decimal(str(s[6])))
            for s in stations
        ]
        self.grid = {}  # (cell_y, cell_x) -> [station positions in self.stations]
        for i, s in enumerate(self.stations):
            self.grid.setdefault(self._cell(s[4], s[5]), []).append(i)
        self.average_price = (
            (sum((s[6] for s in self.stations), Decimal(0)) / len(self.stations))
            if self.stations else None
        )

    @staticmethod
    def _cell(lat, lon):
        """Grid cell (row, column) that contains a point."""
        return math.floor(lat / CELL_DEG), math.floor(lon / CELL_DEG)

    def along_route(self, pts_lat, pts_lon, cum, corridor_miles):
        """
        Stations within `corridor_miles` of the thinned route, sorted by distance from the start.

        pts_lat/pts_lon/cum come from routing.thin_route: one point per mile or so, with
        cum[i] the miles travelled to reach point i.
        """
        if not pts_lat:
            return []

        # --- 1) Bucket the route points into the same grid as the stations. ---
        route_cells = {}
        for v, (la, lo) in enumerate(zip(pts_lat, pts_lon)):
            route_cells.setdefault(self._cell(la, lo), []).append(v)

        # How many cells away a station can still be inside the corridor. cos() is taken at
        # the route's highest latitude, where a degree of longitude is shortest, so the
        # longitude reach is never underestimated anywhere along the route.
        max_lat = max(abs(v) for v in pts_lat)
        reach_y = math.ceil(corridor_miles / (MILES_PER_DEG_LAT * CELL_DEG))
        reach_x = math.ceil(
            corridor_miles / (MILES_PER_DEG_LON * math.cos(math.radians(max_lat)) * CELL_DEG))

        # --- 2) Every station sharing a neighbourhood with some route point. ---
        nearby = set()
        for cy, cx in route_cells:
            for y in range(cy - reach_y, cy + reach_y + 1):
                for x in range(cx - reach_x, cx + reach_x + 1):
                    nearby.update(self.grid.get((y, x), ()))

        # --- 3) For each of those, its closest route point; keep it if inside the corridor. ---
        limit_deg2 = (corridor_miles / MILES_PER_DEG_LAT) ** 2
        result = []
        for sid in nearby:
            s = self.stations[sid]
            s_lat, s_lon = s[4], s[5]
            cy, cx = self._cell(s_lat, s_lon)
            cos_s = math.cos(math.radians(s_lat))
            best_d2, best_v = None, -1
            for y in range(cy - reach_y, cy + reach_y + 1):
                for x in range(cx - reach_x, cx + reach_x + 1):
                    for v in route_cells.get((y, x), ()):
                        dy = s_lat - pts_lat[v]
                        dx = (s_lon - pts_lon[v]) * cos_s
                        d2 = dx * dx + dy * dy
                        if best_d2 is None or d2 < best_d2:
                            best_d2, best_v = d2, v
            if best_d2 is not None and best_d2 <= limit_deg2:
                result.append(Candidate(
                    id=s[0], name=s[1], city=s[2], state=s[3], lat=s_lat, lon=s_lon, price=s[6],
                    mile=cum[best_v], offset=math.sqrt(best_d2) * MILES_PER_DEG_LAT,
                ))
        result.sort(key=lambda c: c.mile)
        return result


# ---------------------------------------------------------------------------
# One index per server process, rebuilt automatically when the data is reloaded.
# ---------------------------------------------------------------------------

_index = None
_index_version = None
_lock = threading.Lock()


def get_station_index():
    """
    Return this process's StationIndex, (re)building it from the DB if missing or outdated.

    load_data bumps a version marker in the cache so every process notices new data. That
    marker is read at most once every few seconds (see safe_cache.get_versioned), and a
    cache outage leaves the in-memory index in place: the index comes from PostgreSQL and
    does not need Redis to keep serving.
    """
    global _index, _index_version
    version = safe_cache.get_versioned(DATA_VERSION_KEY, 0)
    if _index is None or version != _index_version:
        with _lock:
            if _index is None or version != _index_version:
                rows = FuelStation.objects.values_list(
                    "id", "name", "city", "state", "lat", "lon", "price")
                _index = StationIndex([tuple(r) for r in rows])
                _index_version = version
    return _index


def bump_data_version():
    """Call after changing station data so all processes rebuild their index."""
    safe_cache.set(DATA_VERSION_KEY, time.time(), timeout=None)
    safe_cache.reset_versioned(DATA_VERSION_KEY)
