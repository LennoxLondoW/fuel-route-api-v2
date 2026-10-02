"""
Create an API key for a client.

    python manage.py create_api_key "frontend app"
    python manage.py create_api_key "partner X" --expires-days 90

The key is printed once. Only its hash is stored, so it cannot be recovered later
(create a new one and deactivate the old one in the admin instead).
"""

from datetime import timedelta

from django.core.management.base import BaseCommand
from django.utils import timezone

from planner.models import APIKey


class Command(BaseCommand):
    """The "create_api_key" management command."""

    help = "Create a new API key and print it once."

    def add_arguments(self, parser):
        """Command-line arguments: the key's name and an optional lifetime."""
        parser.add_argument("name", help="Who/what the key is for")
        parser.add_argument("--expires-days", type=int, default=None, help="Expire after N days")

    def handle(self, *args, **options):
        """Generate, save (hashed) and print the key."""
        key = APIKey(name=options["name"])
        if options["expires_days"]:
            key.expires_at = timezone.now() + timedelta(days=options["expires_days"])
        raw_key = key.set_new_key()
        key.save()

        self.stdout.write(self.style.SUCCESS(f"API key for '{key.name}' (store it safely, it is shown only once):"))
        self.stdout.write(raw_key)
