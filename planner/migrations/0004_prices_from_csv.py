"""
Re-seed the fuel stations from the supplied price CSV.

Until now the stations came from a file the browser prototype had derived from
fuel-prices-for-be-assessment.csv. Deriving it had lost data: the prototype keyed truck stops on
(name, city, state), so sites listed twice under a long and a short form of the same brand name
collapsed together, and a handful of cities missing from the city table were dropped with no
record. That prototype has since been removed.

The loader now reads the CSV itself and keys on its OPIS Truckstop ID, which is the file's own
identifier for a site. Every priced US truck stop in the file now reaches the planner.

Because the key changes from a content hash to `opis:<id>`, this one reload re-inserts every
station row. Ids are stable from here on.
"""

from django.db import migrations


def reseed(apps, schema_editor):
    """Reload both tables from the price CSV, replacing the stations the prototype had derived."""
    from planner.data_loader import load

    City = apps.get_model("planner", "City")
    FuelStation = apps.get_model("planner", "FuelStation")
    cities, stations = load(City, FuelStation)
    print(f"\n  Loaded {cities} cities and {stations} fuel stations from the price CSV.", end="")


def noop(apps, schema_editor):
    """
    Reverse: nothing to undo.

    Going back would mean re-running the prototype's loader, which no longer exists. Migration
    0002 is what seeds a fresh database, so reversing this one simply leaves the CSV-derived
    rows in place.
    """


class Migration(migrations.Migration):
    """Loads fuel stations from fuel-prices-for-be-assessment.csv."""

    dependencies = [("planner", "0003_fuelstation_external_key")]

    operations = [migrations.RunPython(reseed, noop)]
