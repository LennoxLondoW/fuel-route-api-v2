"""
Give every fuel station a stable identity.

Before this, `load_data` emptied the table and re-inserted it, so every price refresh handed
out fresh primary keys and the `station_id` in an API response was good only until the next
reload. `external_key` is derived from the station's own details, which lets the loader update
rows in place.

Three steps, because the column cannot be unique while it is still empty:
  1. add it, nullable;
  2. fill it in, numbering repeated details 0, 1, 2... in primary-key order, which is the
     order migration 0002 inserted the file in;
  3. make it unique and non-null.
"""

from collections import Counter

from django.db import migrations, models

from planner.models import make_station_external_key

BATCH = 2000


def backfill(apps, schema_editor):
    """Derive a key for every existing row, matching what the loader will compute."""
    FuelStation = apps.get_model("planner", "FuelStation")
    seen = Counter()
    pending = []
    # order_by("pk") reproduces the file order that 0002 inserted, so repeated details get
    # the same occurrence numbers the loader will assign on the next reload.
    for station in FuelStation.objects.order_by("pk").iterator(chunk_size=BATCH):
        details = (station.name, station.city, station.state, station.lat, station.lon)
        station.external_key = make_station_external_key(*details, occurrence=seen[details])
        seen[details] += 1
        pending.append(station)
        if len(pending) >= BATCH:
            FuelStation.objects.bulk_update(pending, ["external_key"])
            pending.clear()
    if pending:
        FuelStation.objects.bulk_update(pending, ["external_key"])


def clear(apps, schema_editor):
    """Reverse: nothing to undo beyond dropping the column, which the AddField reversal does."""


class Migration(migrations.Migration):
    """Adds and backfills FuelStation.external_key, then makes it unique."""

    dependencies = [("planner", "0002_seed_data")]

    operations = [
        migrations.AddField(
            model_name="fuelstation",
            name="external_key",
            field=models.CharField(editable=False, max_length=40, null=True),
        ),
        migrations.RunPython(backfill, clear),
        migrations.AlterField(
            model_name="fuelstation",
            name="external_key",
            field=models.CharField(
                editable=False, max_length=40, unique=True,
                help_text="Stable identity from the data file; keeps the row's id across reloads."),
        ),
    ]
