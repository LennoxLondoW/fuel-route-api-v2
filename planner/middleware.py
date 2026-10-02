"""Custom middleware: request ids, API security headers and request logging."""

import logging
import re
import time
import uuid

logger = logging.getLogger("planner.requests")

# Accept a caller-supplied request id only if it looks safe (no header/log injection).
SAFE_REQUEST_ID = re.compile(r"^[A-Za-z0-9\-]{8,64}$")


class RequestIDMiddleware:
    """
    - Gives every request an id (request.request_id), returned in the X-Request-ID header
      and in error bodies, so a client's bug report can be matched to server logs.
    - Adds strict headers to API responses (they are JSON, never HTML to render or cache).
    - Logs one line per API request.
    """

    def __init__(self, get_response):
        """Called once at server start; get_response is the next layer (middleware or view)."""
        self.get_response = get_response

    def __call__(self, request):
        """Called for every request: tag it, pass it on, then decorate the response."""
        incoming = request.headers.get("X-Request-ID", "")
        request.request_id = incoming if SAFE_REQUEST_ID.match(incoming) else uuid.uuid4().hex

        started = time.perf_counter()
        response = self.get_response(request)
        elapsed_ms = (time.perf_counter() - started) * 1000

        response["X-Request-ID"] = request.request_id

        if request.path.startswith("/api/"):
            # Results are per-client: don't let browsers/proxies store them.
            response["Cache-Control"] = "no-store"
            # A JSON API never needs to load scripts, styles, frames...
            response["Content-Security-Policy"] = "default-src 'none'; frame-ancestors 'none'"
            # Log the path only: the query string contains user-entered locations.
            logger.info("%s %s %s %.0fms id=%s", request.method, request.path,
                        response.status_code, elapsed_ms, request.request_id)

        return response
