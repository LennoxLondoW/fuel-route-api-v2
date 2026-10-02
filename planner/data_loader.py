"""
Fill the City and FuelStation tables from the project's data files. Used by:
  - migration 0002_seed_data        (seeds the tables on "manage.py migrate")
  - migration 0004_prices_from_csv  (re-seeds once, after the schema is final)
  - the load_data command           (reloads them later, e.g. with new prices)

Sources:
    fuel-prices-for-be-assessment.csv   the supplied price list: this is the authority on
                                        which truck stops exist and what they charge
    us-cities.csv                       US cities with coordinates, used both to geocode
                                        "City, ST" input and to place the truck stops
    city-supplement.json                the city names the price list uses that us-cities.csv
                                        does not have, so no priced truck stop is dropped

The price file has no coordinates, only a city and state, so every station is placed at its
city's centre. That is why the planner treats stations as being within a corridor of the route
rather than at an exact point.

Rows are updated in place, keyed on the price file's own OPIS Truckstop ID, so a station keeps
its primary key across reloads and the `station_id` in an API response stays meaningful.
"""

import csv
import json
import logging
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from django.conf import settings

from .services.geocoding import normalize

BASE_DIR = Path(settings.BASE_DIR)
DEFAULT_CITIES_FILE = BASE_DIR / "us-cities.csv"
DEFAULT_PRICES_FILE = BASE_DIR / "fuel-prices-for-be-assessment.csv"
DEFAULT_SUPPLEMENT_FILE = BASE_DIR / "city-supplement.json"
BATCH_SIZE = 5000

# The price file lists some truck stops more than once under the same OPIS id, usually a long
# and a short form of the brand name at slightly different prices, with no date to tell them
# apart. We keep the longest name and the lowest price: the lowest is a real quoted price for
# that site and this is a planner for cheap fuel. Set to max or mean here to change that.
DUPLICATE_PRICE = min

logger = logging.getLogger(__name__)


class DataFileError(Exception):
    """A data file is missing or not in the expected format."""


def read_cities(path=DEFAULT_CITIES_FILE):
    """
    Parse us-cities.csv into dicts of City fields.

    Columns: city, state, lat, lon. Names are normalized here exactly as user input will be,
    so "St. Louis, MO" typed into the API matches the row stored for "saint louis".
    """
    path = Path(path)
    if not path.exists():
        raise DataFileError(f"Data file not found: {path}")

    required = {"city", "state", "lat", "lon"}
    cities = []
    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise DataFileError(f"{path}: missing column(s) {sorted(missing)}.")
        for row in reader:
            try:
                lat, lon = float(row["lat"]), float(row["lon"])
            except (TypeError, ValueError):
                continue
            cities.append({"name": normalize(row["city"]), "state": row["state"].strip().lower(),
                           "lat": lat, "lon": lon})
    return cities


def read_supplement(path=DEFAULT_SUPPLEMENT_FILE):
    """
    Parse city-supplement.json into (aliases, coordinates).

    Missing file is not an error: the supplement only improves coverage, and the loader
    reports anything it could not place either way.
    """
    path = Path(path)
    if not path.exists():
        return {}, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise DataFileError(f"{path}: not valid JSON ({exc}).") from exc
    return data.get("aliases", {}), data.get("coordinates", {})


def read_prices(path=DEFAULT_PRICES_FILE):
    """
    Parse the supplied price CSV into one entry per truck stop.

    Returns a list of dicts with the OPIS id, name, city, state and price. Every truck stop in
    the file is kept, including the 112 in Canadian provinces: a route near the border can
    legitimately pass one, and discarding priced stations is not this function's call to make.
    Only rows with no state at all, or a price that will not parse, are skipped, and both are
    counted in the log so nothing disappears unexamined.
    """
    path = Path(path)
    if not path.exists():
        raise DataFileError(f"Data file not found: {path}")

    required = {"OPIS Truckstop ID", "Truckstop Name", "City", "State", "Retail Price"}
    grouped = defaultdict(list)
    no_state = 0
    bad_price = 0

    with path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        missing = required - set(reader.fieldnames or ())
        if missing:
            raise DataFileError(f"{path}: missing column(s) {sorted(missing)}.")

        for row in reader:
            state = (row["State"] or "").strip().upper()
            if not state:
                no_state += 1
                continue
            try:
                price = Decimal(str(row["Retail Price"]).strip())
            except (ArithmeticError, TypeError, ValueError):
                bad_price += 1
                continue
            if price <= 0:
                bad_price += 1
                continue
            grouped[(row["OPIS Truckstop ID"] or "").strip()].append({
                "name": (row["Truckstop Name"] or "").strip(),
                "city": (row["City"] or "").strip(),
                "state": state,
                "price": price,
            })

    if no_state or bad_price:
        logger.warning("Price file: skipped %s row(s) with no state and %s with an unusable price.",
                       no_state, bad_price)

    stations = []
    for opis_id, entries in grouped.items():
        if not opis_id:
            continue
        best = max(entries, key=lambda e: len(e["name"]))   # longest name variant
        stations.append({
            "external_key": f"opis:{opis_id}",
            "name": best["name"][:200],
            "city": best["city"][:100],
            "state": best["state"][:2],
            "price": DUPLICATE_PRICE(e["price"] for e in entries).quantize(Decimal("0.001")),
        })
    return stations


def locate_stations(stations, cities, aliases=None, coordinates=None):
    """
    Give each station the coordinates of its city.

    The price file has no coordinates, so a station is placed at the centre of the city it
    names. Lookup order: the city table, then a supplement alias pointing at a city that is in
    the table under another spelling, then a supplement coordinate pair.

    Returns (located, unplaced). `unplaced` lists the stations no source could place, so the
    caller can report them instead of dropping them silently.
    """
    aliases = aliases or {}
    coordinates = coordinates or {}
    table = {(c["name"], c["state"]): (c["lat"], c["lon"]) for c in cities}

    located, unplaced = [], []
    for station in stations:
        state = station["state"].lower()
        key = f"{normalize(station['city'])}|{state}"

        point = table.get((normalize(station["city"]), state))
        if point is None and key in aliases:
            point = table.get((normalize(aliases[key]), state))
        if point is None and key in coordinates:
            point = tuple(coordinates[key])

        if point is None:
            unplaced.append(station)
            continue
        located.append({**station, "lat": float(point[0]), "lon": float(point[1])})
    return located, unplaced


def _dedupe(rows, key_fields):
    """Keep the last row for each key, so one INSERT never lists the same conflict twice."""
    unique = {}
    for row in rows:
        unique[tuple(row[f] for f in key_fields)] = row
    return list(unique.values())


def load(city_model, station_model, cities_file=DEFAULT_CITIES_FILE,
         prices_file=DEFAULT_PRICES_FILE, supplement_file=DEFAULT_SUPPLEMENT_FILE):
    """
    Bring both tables in line with the data files, updating rows in place.

    The model classes are parameters so a migration can pass its historical models. Migration
    0002 runs before `external_key` exists, so its absence is detected rather than assumed.
    Call inside a transaction (migrations already are one). Returns (cities, stations) counts.
    """
    cities = _dedupe(read_cities(cities_file), ("name", "state"))
    aliases, coordinates = read_supplement(supplement_file)
    stations, unplaced = locate_stations(read_prices(prices_file), cities, aliases, coordinates)

    if unplaced:
        # Loud on purpose: a dropped truck stop is a price the planner can never pick.
        logger.warning(
            "Could not place %s truck stop(s) from the price file; add them to %s. %s",
            len(unplaced), Path(supplement_file).name,
            ", ".join(sorted({f"{s['city']}, {s['state']}" for s in unplaced})))

    # Cities are keyed by (name, state): refresh the coordinates of the ones already there.
    city_model.objects.bulk_create(
        [city_model(**c) for c in cities],
        batch_size=BATCH_SIZE,
        update_conflicts=True,
        update_fields=["lat", "lon"],
        unique_fields=["name", "state"],
    )

    # Stations are keyed by external_key, which migration 0002's historical model lacks.
    has_external_key = any(f.name == "external_key" for f in station_model._meta.get_fields())
    if has_external_key:
        station_model.objects.bulk_create(
            [station_model(**s) for s in stations],
            batch_size=BATCH_SIZE,
            update_conflicts=True,
            update_fields=["name", "city", "state", "lat", "lon", "price"],
            unique_fields=["external_key"],
        )
        # Drop stations that are no longer in the price file.
        station_model.objects.exclude(
            external_key__in=[s["external_key"] for s in stations]).delete()
    else:
        station_model.objects.all().delete()
        station_model.objects.bulk_create(
            [station_model(**{k: v for k, v in s.items() if k != "external_key"})
             for s in stations],
            batch_size=BATCH_SIZE)

    return city_model.objects.count(), station_model.objects.count()
