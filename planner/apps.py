"""Django app configuration for the fuel route planner."""

from django.apps import AppConfig


class PlannerConfig(AppConfig):
    """Registers the "planner" app (models, admin, API views, management commands)."""

    default_auto_field = "django.db.models.BigAutoField"
    name = "planner"
    verbose_name = "Fuel route planner"
