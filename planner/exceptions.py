"""
Errors raised by the API, and the single error format used for every failure:

    {
      "error": {
        "code": "location_not_found",          <- stable, machine-readable
        "message": "Could not find ...",       <- human-readable
        "details": {...},                      <- optional (e.g. per-field validation errors)
        "request_id": "3f2a..."                <- quote this when reporting a problem
      }
    }

The code that turns exceptions into that format lives in handlers.py.
(It is kept separate because it imports rest_framework.views, which would cause
a circular import when authentication.py imports these exception classes.)
"""

from rest_framework import status
from rest_framework.exceptions import APIException


class LocationNotFound(APIException):
    """The geocoder could not resolve a place inside the USA."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "location_not_found"
    default_detail = "Could not find that location in the USA."


class RouteNotFound(APIException):
    """OSRM found no drivable road between the two points."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "route_not_found"
    default_detail = "No driving route exists between these locations."


class NoStationInRange(APIException):
    """The trip has a gap longer than the vehicle's range with no fuel station."""
    status_code = status.HTTP_422_UNPROCESSABLE_ENTITY
    default_code = "no_station_in_range"
    default_detail = "The trip cannot be completed: no fuel station within range."


class UpstreamUnavailable(APIException):
    """A third-party service (routing / geocoding) failed or timed out."""
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_code = "upstream_unavailable"
    default_detail = "A routing/geocoding service is unavailable. Please retry shortly."


class TooManyAuthFailures(APIException):
    """This IP sent too many invalid API keys recently (brute-force protection)."""
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    default_code = "too_many_auth_failures"
    default_detail = "Too many invalid API key attempts. Try again later."


def error_body(code, message, request_id=None, details=None):
    """Build the standard {"error": {...}} body."""
    error = {"code": code, "message": message}
    if details:
        error["details"] = details
    if request_id:
        error["request_id"] = request_id
    return {"error": error}
