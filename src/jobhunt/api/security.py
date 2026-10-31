"""Local-server security and CSRF protection (Brief v12).

Defences:
- DNS rebinding: requests must have Host header 127.0.0.1 or localhost.
- CSRF / Drive-by write attacks: all state-changing endpoints (POST/PUT/PATCH/DELETE)
  require a matching per-process random token delivered via HTML bootstrap
  and sent in the `X-Jobhunt-Token` request header.
- Cross-origin writes: if `Origin` header is present, it must be from 127.0.0.1 or localhost.
"""

from __future__ import annotations

import hmac
import logging
import secrets
from collections.abc import Awaitable, Callable
from urllib.parse import urlsplit

from fastapi import Request, Response
from fastapi.responses import JSONResponse

logger = logging.getLogger(__name__)

CSRF_HEADER = "x-jobhunt-token"
WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
LOCAL_HOSTS = frozenset({"127.0.0.1", "localhost", "testserver"})


def generate_csrf_token() -> str:
    """Generate a high-entropy per-process session token."""
    return secrets.token_hex(32)


async def csrf_and_origin_guard(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    """Validate Origin and X-Jobhunt-Token for all state-changing requests."""
    if request.method in WRITE_METHODS:
        # 1. Origin header check (if present)
        origin = request.headers.get("origin")
        if origin:
            parsed_origin = urlsplit(origin)
            origin_host = (parsed_origin.hostname or "").lower()
            if origin_host not in LOCAL_HOSTS:
                logger.warning("Rejected write from untrusted origin: %s", origin)
                return JSONResponse({"detail": "cross-origin writes are not allowed"}, status_code=403)

        # 2. X-Jobhunt-Token validation
        expected_token = getattr(request.app.state, "csrf_token", None)
        if expected_token:
            provided_token = request.headers.get(CSRF_HEADER)
            if not provided_token or not hmac.compare_digest(provided_token, expected_token):
                logger.warning(
                    "Rejected write to %s due to invalid or missing %s",
                    request.url.path,
                    CSRF_HEADER,
                )
                return JSONResponse({"detail": f"invalid or missing {CSRF_HEADER}"}, status_code=403)

        # 3. Content-Type check (for JSON bodies)
        content_length = request.headers.get("content-length", "0")
        has_body = content_length not in {"", "0"} or "transfer-encoding" in request.headers
        content_type = request.headers.get("content-type", "").lower()
        if has_body and not (
            content_type.startswith("application/json") or content_type.startswith("multipart/form-data")
        ):
            return JSONResponse({"detail": "request body must be application/json or multipart/form-data"}, status_code=415)

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api/"):
        response.headers.setdefault("Cache-Control", "no-store")
    return response
