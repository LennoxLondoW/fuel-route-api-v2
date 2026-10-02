"""
Database tables (PostgreSQL):
    City         US cities with coordinates, for offline geocoding
    FuelStation  truck stops with their fuel price
    APIKey       client credentials (only a hash of each key is stored)
"""

import hashlib
import secrets

from django.db import models
from django.utils import timezone


class City(models.Model):
    """
    A US city with coordinates, used to geocode "City, ST" inputs without any network call.
    Names are stored normalized (lowercase, "saint" -> "st", ...) - see services/geocoding.normalize().
    """

    name = models.CharField(max_length=100)
    state = models.CharField(max_length=2)  # lowercase two-letter code, e.g. "ny"
    lat = models.FloatField()
    lon = models.FloatField()

    class Meta:
        """Table options: admin label and the (name, state) uniqueness rule."""

        verbose_name_plural = "cities"
        constraints = [
            # One row per (name, state); also creates the index used for lookups.
            models.UniqueConstraint(fields=["name", "state"], name="unique_city_per_state"),
        ]

    def __str__(self):
        """Readable label for the admin, e.g. "New York, NY"."""
        return f"{self.name.title()}, {self.state.upper()}"


class FuelStation(models.Model):
    """
    A truck stop with its diesel price. Coordinates are city-level (from the price file's city).

    `external_key` is the price file's own OPIS Truckstop ID, prefixed "opis:". Reloading the
    file therefore updates rows in place instead of deleting and re-inserting them. Without it
    every reload renumbered the table, and the `station_id` the API hands to clients meant
    nothing the next day.
    """

    name = models.CharField(max_length=200)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=2)  # uppercase two-letter code, e.g. "TX"
    lat = models.FloatField()
    lon = models.FloatField()
    price = models.DecimalField(max_digits=6, decimal_places=3)  # USD per gallon
    external_key = models.CharField(
        max_length=40, unique=True, editable=False,
        help_text="Stable identity from the data file; keeps the row's id across reloads.")

    class Meta:
        """Table options: index on state for admin filtering."""

        indexes = [models.Index(fields=["state"])]

    def __str__(self):
        """Readable label for the admin, e.g. "Pilot #123 (Dallas, TX) $3.199"."""
        return f"{self.name} ({self.city}, {self.state}) ${self.price}"


def make_station_external_key(name, city, state, lat, lon, occurrence=0):
    """
    Stable key for one row of the price file.

    Superseded by the OPIS Truckstop ID once the loader began reading the price CSV directly,
    and kept only because migration 0003 backfills with it. New rows are keyed "opis:<id>".

    Module level on purpose, so a data migration can import it. Historical models carry
    fields but never custom methods.
    """
    raw = f"{name}|{city}|{state}|{lat:.4f}|{lon:.4f}|{occurrence}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:40]


class APIKey(models.Model):
    """
    A client credential. Only a SHA-256 hash of the key is stored: if the database leaks,
    the keys themselves are not exposed. The raw key is shown exactly once, at creation time.
    """

    KEY_PREFIX = "fr_"  # makes keys easy to recognise (e.g. by secret scanners)

    name = models.CharField(max_length=100, help_text="Who/what this key is for.")
    prefix = models.CharField(max_length=12, editable=False, db_index=True,
                              help_text="First characters of the key, to identify it in logs.")
    hashed_key = models.CharField(max_length=64, unique=True, editable=False)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True, help_text="Leave empty for no expiry.")
    last_used_at = models.DateTimeField(null=True, blank=True, editable=False)

    class Meta:
        """Table options: admin label."""

        verbose_name = "API key"

    def __str__(self):
        """Readable label: the key's name plus its visible prefix (never the full key)."""
        return f"{self.name} ({self.prefix}…)"

    @staticmethod
    def hash(raw_key):
        """
        SHA-256 hex digest of a raw key. A fast hash is fine here (unlike for passwords)
        because keys are 256-bit random values, far too many to guess.
        """
        return hashlib.sha256(raw_key.encode()).hexdigest()

    def set_new_key(self):
        """Generate a new random key, store its hash, and return the raw key (show it once!)."""
        raw_key = self.KEY_PREFIX + secrets.token_urlsafe(32)  # 256 bits of randomness
        self.prefix = raw_key[:10]
        self.hashed_key = self.hash(raw_key)
        return raw_key

    @property
    def is_expired(self):
        """True once expires_at has passed (keys without expires_at never expire)."""
        return self.expires_at is not None and self.expires_at <= timezone.now()
