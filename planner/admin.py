"""Django admin screens for browsing data and managing API keys."""

from django.contrib import admin, messages

from .models import APIKey, City, FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    """Browse/edit stations, cheapest first."""

    list_display = ("name", "city", "state", "price")
    list_filter = ("state",)
    search_fields = ("name", "city")
    ordering = ("price",)


@admin.register(City)
class CityAdmin(admin.ModelAdmin):
    """Browse the offline geocoding table."""

    list_display = ("name", "state", "lat", "lon")
    list_filter = ("state",)
    search_fields = ("name",)


@admin.register(APIKey)
class APIKeyAdmin(admin.ModelAdmin):
    """Create, deactivate and expire API keys. The key hash is never shown."""

    list_display = ("name", "prefix", "is_active", "created_at", "expires_at", "last_used_at")
    list_filter = ("is_active",)
    search_fields = ("name", "prefix")
    readonly_fields = ("prefix", "created_at", "last_used_at")
    fields = ("name", "is_active", "expires_at", "prefix", "created_at", "last_used_at")

    def save_model(self, request, obj, form, change):
        """On creation, generate the key and display it once in a message."""
        if not change:
            # New key: generate it and show it ONCE. Only its hash is saved.
            raw_key = obj.set_new_key()
            messages.warning(request, f"Copy this API key now, it will not be shown again: {raw_key}")
        super().save_model(request, obj, form, change)
