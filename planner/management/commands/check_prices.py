"""
Check that every truck stop in the price file can be placed on the map, and fill the gaps.

    python manage.py check_prices                    # report coverage, change nothing
    python manage.py check_prices --geocode-missing  # look the gaps up and write the supplement

A truck stop the loader cannot place is a price the planner can never pick, so the goal is a
report with nothing in it. The price file names a city and state but gives no coordinates, so
coverage depends on us-cities.csv plus city-supplement.json.

`--geocode-missing` asks Nominatim for each unplaced city, one request per second as its usage
policy requires, and keeps an answer only when the province or state it comes back with is the
one the price file asked for. That check matters: "Windsor, ON" and "Windsor, CA" are different
places, and placing a station in the wrong one would quietly corrupt every route near it.
"""

import json
import time
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from planner.data_loader import (DEFAULT_CITIES_FILE, DEFAULT_PRICES_FILE,
                                 DEFAULT_SUPPLEMENT_FILE, DataFileError, locate_stations,
                                 read_cities, read_prices, read_supplement)
from planner.services.geocoding import normalize
from planner.services.http import get_json

# Nominatim's public server allows one request a second. Leave a margin.
REQUEST_INTERVAL_SECONDS = 1.2
# Only look in the two countries the price file actually covers.
COUNTRY_CODES = "us,ca"


class Command(BaseCommand):
    """The "check_prices" management command."""

    help = "Report truck stops the loader cannot place, and optionally geocode them."

    def add_arguments(self, parser):
        """Command-line options: where the data files are, and whether to write."""
        parser.add_argument("--prices", default=str(DEFAULT_PRICES_FILE))
        parser.add_argument("--cities", default=str(DEFAULT_CITIES_FILE))
        parser.add_argument("--supplement", default=str(DEFAULT_SUPPLEMENT_FILE))
        parser.add_argument("--geocode-missing", action="store_true",
                            help="Look up unplaced cities and add them to the supplement.")

    def handle(self, *args, **options):
        """Report coverage, then optionally close the gap and report it again."""
        try:
            stations = read_prices(options["prices"])
            cities = read_cities(options["cities"])
            aliases, coordinates = read_supplement(options["supplement"])
        except DataFileError as exc:
            raise CommandError(str(exc)) from exc

        located, unplaced = locate_stations(stations, cities, aliases, coordinates)
        self._report(stations, located, unplaced)

        if not unplaced or not options["geocode_missing"]:
            if unplaced:
                self.stdout.write("\nRe-run with --geocode-missing to look these up.")
            return

        added = self._geocode(unplaced, coordinates)
        if added:
            self._write_supplement(Path(options["supplement"]), coordinates)
            self.stdout.write(self.style.SUCCESS(
                f"\nAdded {added} city/cities to {Path(options['supplement']).name}."))
            located, unplaced = locate_stations(stations, cities, aliases, coordinates)
            self.stdout.write("")
            self._report(stations, located, unplaced)

    def _report(self, stations, located, unplaced):
        """Print how many truck stops were placed, and name the cities behind any that were not."""
        self.stdout.write(f"Truck stops in the price file : {len(stations)}")
        self.stdout.write(f"Placed on the map             : {len(located)}")
        style = self.style.SUCCESS if not unplaced else self.style.WARNING
        self.stdout.write(style(f"Unplaced                      : {len(unplaced)}"))

        if unplaced:
            missing = sorted({(s["city"], s["state"]) for s in unplaced})
            self.stdout.write(f"\nCities with no coordinates ({len(missing)}):")
            for city, state in missing:
                self.stdout.write(f"   {city}, {state}")

    def _geocode(self, unplaced, coordinates):
        """Look up each unplaced city and record the ones that come back in the right region."""
        missing = sorted({(s["city"], s["state"]) for s in unplaced})
        self.stdout.write(f"\nGeocoding {len(missing)} city/cities, "
                          f"about {len(missing) * REQUEST_INTERVAL_SECONDS / 60:.0f} minute(s)...")
        added = 0
        for city, state in missing:
            point = self._lookup(city, state)
            if point is None:
                self.stdout.write(self.style.WARNING(f"   {city}, {state}: no confident match"))
                continue
            coordinates[f"{normalize(city)}|{state.lower()}"] = list(point)
            self.stdout.write(f"   {city}, {state}: {point[0]}, {point[1]}")
            added += 1
            time.sleep(REQUEST_INTERVAL_SECONDS)
        return added

    def _lookup(self, city, state):
        """
        One Nominatim query. Returns (lat, lon), or None if the answer is not clearly right.

        The result is accepted only when its ISO 3166-2 subdivision matches the state or
        province the price file gave, so a same-named place elsewhere is never used.
        """
        cfg = settings.FUELROUTE
        try:
            _, data = get_json(
                f"{cfg['NOMINATIM_URL']}/search",
                params={"q": f"{city}, {state}", "format": "jsonv2", "limit": 5,
                        "countrycodes": COUNTRY_CODES, "addressdetails": 1},
                headers={"User-Agent": cfg["NOMINATIM_USER_AGENT"]},
            )
        except Exception as exc:
            self.stderr.write(self.style.WARNING(f"   {city}, {state}: lookup failed ({exc})"))
            return None

        if not isinstance(data, list):
            return None
        for hit in data:
            address = hit.get("address") or {}
            subdivision = ""
            for key, value in address.items():
                if key.startswith("ISO3166-2-lvl"):
                    subdivision = str(value)
                    break
            if not subdivision.endswith(f"-{state.upper()}"):
                continue
            try:
                return round(float(hit["lat"]), 4), round(float(hit["lon"]), 4)
            except (KeyError, TypeError, ValueError):
                continue
        return None

    @staticmethod
    def _write_supplement(path, coordinates):
        """Rewrite the supplement, keeping its comment and aliases and sorting the coordinates."""
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        data["coordinates"] = {k: coordinates[k] for k in sorted(coordinates)}
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
