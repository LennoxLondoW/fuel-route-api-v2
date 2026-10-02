"""
Reload cities and fuel stations from the data files (the tables are first seeded during
"manage.py migrate"; use this command when the files change, e.g. with new prices).

    python manage.py load_data
    python manage.py load_data --prices other-prices.csv

Stations are matched to existing rows on the price file's OPIS Truckstop ID, so reloading
updates prices in place and no station changes its id.

Runs in one transaction, so the API never sees a half-loaded table.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from planner.data_loader import (DEFAULT_CITIES_FILE, DEFAULT_PRICES_FILE,
                                 DEFAULT_SUPPLEMENT_FILE, DataFileError, load)
from planner.models import City, FuelStation
from planner.services.stations import bump_data_version


class Command(BaseCommand):
    """The "load_data" management command."""

    help = "Reload cities and fuel stations from the price CSV and the city table."

    def add_arguments(self, parser):
        """Command-line options: where each of the three data files lives."""
        parser.add_argument("--prices", default=str(DEFAULT_PRICES_FILE),
                            help="CSV of truck stop fuel prices")
        parser.add_argument("--cities", default=str(DEFAULT_CITIES_FILE),
                            help="us-cities.csv, the city coordinate table")
        parser.add_argument("--supplement", default=str(DEFAULT_SUPPLEMENT_FILE),
                            help="JSON of city aliases and extra coordinates")

    def handle(self, *args, **options):
        """Bring both tables in line with the files, then tell running servers to refresh."""
        try:
            with transaction.atomic():
                cities, stations = load(
                    City, FuelStation,
                    cities_file=options["cities"],
                    prices_file=options["prices"],
                    supplement_file=options["supplement"],
                )
        except DataFileError as exc:
            raise CommandError(str(exc)) from exc

        # Tell running API processes to rebuild their in-memory station index.
        try:
            bump_data_version()
        except Exception as exc:  # cache down: processes will pick it up after a restart
            self.stderr.write(self.style.WARNING(f"Could not notify cache: {exc}"))

        self.stdout.write(self.style.SUCCESS(f"Loaded {cities} cities and {stations} stations."))
