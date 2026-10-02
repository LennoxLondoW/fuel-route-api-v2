"""API routes for version 1."""

from django.urls import path

from . import views

# Mounted under /api/v1/ (see config/urls.py). A breaking change would go under /api/v2/.
urlpatterns = [
    path("route/", views.RoutePlanView.as_view(), name="route-plan"),
    path("health/", views.HealthView.as_view(), name="health"),
]
