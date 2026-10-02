"""
Top-level URLs:
    /admin/    Django admin (manage stations, cities, API keys)
    /api/v1/   the JSON API
    /          demo map page that calls the API
"""

from django.contrib import admin
from django.urls import include, path

from planner.views import DemoPageView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("planner.urls")),
    path("", DemoPageView.as_view(), name="demo"),
]

# Return JSON instead of HTML error pages (active when DEBUG=False).
handler404 = "planner.views.json_404"
handler500 = "planner.views.json_500"
