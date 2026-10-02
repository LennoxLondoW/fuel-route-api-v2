"""
DRF exception handler (wired up in settings.REST_FRAMEWORK["EXCEPTION_HANDLER"]).
Converts every error raised inside an API view into the format described in exceptions.py.
"""

import logging

from rest_framework import status
from rest_framework.exceptions import Throttled, ValidationError
from rest_framework.response import Response
from rest_framework.views import exception_handler

from .exceptions import error_body

logger = logging.getLogger(__name__)


def api_exception_handler(exc, context):
    """Convert any exception raised in a DRF view into the standard error format."""
    request = context.get("request")
    request_id = getattr(request, "request_id", None)

    # Let DRF turn known exceptions (incl. Django's Http404/PermissionDenied) into a response.
    response = exception_handler(exc, context)

    if response is None:
        # Unexpected bug: log the traceback, but never leak internals to the client.
        logger.exception("Unhandled error (request_id=%s)", request_id)
        return Response(
            error_body("internal_error", "An unexpected error occurred.", request_id),
            status=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )

    if isinstance(exc, ValidationError):
        code, message, details = "validation_error", "Invalid input.", response.data
    else:
        code = getattr(exc, "default_code", "error")
        message = str(getattr(exc, "detail", "Error."))
        details = None

    if isinstance(exc, Throttled):
        code = "rate_limited"
        details = {"retry_after_seconds": exc.wait and round(exc.wait)}

    response.data = error_body(code, message, request_id, details)
    return response
