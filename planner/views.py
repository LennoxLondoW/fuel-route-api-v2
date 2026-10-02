"""HTTP endpoints: the route planner API, a health check, the demo page and JSON error pages."""

import logging

from django.conf import settings
from django.db import connection
from django.http import JsonResponse
from django.views.generic import TemplateView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from .exceptions import error_body
from .serializers import RoutePlanRequestSerializer
from .services import safe_cache
from .services.trip import plan_trip
from .throttling import HealthThrottle

logger = logging.getLogger(__name__)


class RoutePlanView(APIView):
    """
    Plan a trip with the cheapest fuel stops.

        GET  /api/v1/route/?from=New York, NY&to=Los Angeles, CA
        POST /api/v1/route/   {"from": "New York, NY", "to": "Los Angeles, CA"}

    Auth: "X-API-Key" header. Rate limited per key.
    """

    def get(self, request):
        """Inputs come from the query string."""
        return self._plan(request.query_params)

    def post(self, request):
        """Inputs come from a JSON body."""
        return self._plan(request.data)

    def _plan(self, data):
        """Validate the inputs, run the planner, and return the result as JSON."""
        serializer = RoutePlanRequestSerializer(data=data)
        serializer.is_valid(raise_exception=True)  # -> 400 with per-field details
        v = serializer.validated_data
        result = plan_trip(v["from"], v["to"], geometry=v["geometry"])
        result["request_id"] = self.request.request_id
        return Response(result)


class HealthView(APIView):
    """
    Liveness/readiness probe for load balancers and monitoring.

    Public (no key) and throttled on its own generous scope, so a fleet of probes sharing one
    source address cannot rate-limit itself into looking unhealthy. Reveals only "ok"/"error".

    The database is required: without it nothing can be planned, so a failure is a 503. The
    cache is not: the API runs without it, just slower and without rate limiting, so a cache
    failure reports "degraded" and still answers 200.
    """

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [HealthThrottle]

    def get(self, request):
        """200 if PostgreSQL answers, 503 if it does not. Cache trouble only degrades."""
        checks = {"database": "ok", "cache": "ok"}

        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
        except Exception:
            logger.exception("Health check: database down")
            checks["database"] = "error"

        if not safe_cache.ping():
            logger.warning("Health check: cache down (serving in degraded mode)")
            checks["cache"] = "error"

        healthy = checks["database"] == "ok"
        status_text = "ok" if all(v == "ok" for v in checks.values()) else "degraded"
        return Response({"status": status_text if healthy else "error", "checks": checks},
                        status=200 if healthy else 503)


class DemoPageView(TemplateView):
    """A small Leaflet page that calls the API and draws the result (see templates/planner/demo.html)."""

    template_name = "planner/demo.html"

    def get_context_data(self, **kwargs):
        """Pre-fill the key field with DEMO_API_KEY from .env (demo convenience: anyone who opens the page can see it)."""
        context = super().get_context_data(**kwargs)
        context["demo_api_key"] = settings.FUELROUTE["DEMO_API_KEY"]
        return context

    def get(self, request, *args, **kwargs):
        """Render the page with a Content-Security-Policy tailored to it."""
        response = super().get(request, *args, **kwargs)
        # Only allow what the page actually uses: our own JS/CSS, Leaflet from cdnjs, OSM map tiles.
        response["Content-Security-Policy"] = (
            "default-src 'self'; "
            "script-src 'self' https://cdnjs.cloudflare.com; "
            "style-src 'self' https://cdnjs.cloudflare.com; "
            "img-src 'self' data: https://cdnjs.cloudflare.com https://tile.openstreetmap.org; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        )
        return response


# JSON replacements for Django's HTML error pages (used when DEBUG=False).

def json_404(request, exception=None):
    """Unknown URL -> JSON 404."""
    body = error_body("not_found", "This endpoint does not exist.", getattr(request, "request_id", None))
    return JsonResponse(body, status=404)


def json_500(request):
    """Crash outside DRF -> JSON 500 (details only in the server log)."""
    body = error_body("internal_error", "An unexpected error occurred.", getattr(request, "request_id", None))
    return JsonResponse(body, status=500)
