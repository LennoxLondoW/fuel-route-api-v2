"""
One safe way to call third-party HTTP services.

- The base URLs come from settings only; user input is only ever sent as encoded query
  parameters or formatted numbers, so it cannot redirect the request elsewhere (SSRF).
- Timeouts, no redirects, and a maximum response size protect us from slow/huge replies.
"""

import json
import logging

import requests
from django.conf import settings

from ..exceptions import UpstreamUnavailable

logger = logging.getLogger(__name__)

# One shared session = connection pooling (much faster for repeated calls).
_session = requests.Session()


def get_json(url, params=None, headers=None, ok_statuses=(200,)):
    """GET a URL and return (status_code, parsed_json). Raises UpstreamUnavailable on any failure."""
    cfg = settings.FUELROUTE
    try:
        with _session.get(
            url,
            params=params,
            headers=headers,
            timeout=cfg["HTTP_TIMEOUT_SECONDS"],
            allow_redirects=False,
            stream=True,  # read the body ourselves so we can cap its size
        ) as resp:
            if resp.status_code not in ok_statuses:
                logger.warning("Upstream %s returned HTTP %s", url, resp.status_code)
                raise UpstreamUnavailable()

            body = bytearray()
            for chunk in resp.iter_content(64 * 1024):
                body.extend(chunk)
                if len(body) > cfg["MAX_UPSTREAM_BYTES"]:
                    logger.warning("Upstream %s response too large", url)
                    raise UpstreamUnavailable()

            return resp.status_code, json.loads(body)

    except (requests.RequestException, ValueError) as exc:  # network error / bad JSON
        logger.warning("Upstream %s failed: %s", url, exc)
        raise UpstreamUnavailable() from exc
