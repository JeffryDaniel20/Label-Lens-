"""Cross-cutting HTTP middleware: correlation ids, security headers, body size cap."""

from __future__ import annotations

import time
import uuid
from collections.abc import Awaitable, Callable

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.platform.errors import problem_response
from app.platform.logging import actor_id_var, correlation_id_var, get_logger, org_id_var

CORRELATION_HEADER = "X-Correlation-Id"
_log = get_logger("http")
_SAFE_ID = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")


def _sanitize_correlation_id(raw: str | None) -> str:
    """Accept a caller-supplied id only if it is short and alphanumeric."""
    if raw and 8 <= len(raw) <= 64 and set(raw) <= _SAFE_ID:
        return raw
    return uuid.uuid4().hex


class CorrelationIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        correlation_id = _sanitize_correlation_id(request.headers.get(CORRELATION_HEADER))
        token = correlation_id_var.set(correlation_id)
        org_token = org_id_var.set(None)
        actor_token = actor_id_var.set(None)
        request.state.correlation_id = correlation_id
        started = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            _log.info(
                "request",
                method=request.method,
                path=request.url.path,
                duration_ms=duration_ms,
            )
            correlation_id_var.reset(token)
            org_id_var.reset(org_token)
            actor_id_var.reset(actor_token)
        response.headers[CORRELATION_HEADER] = correlation_id
        return response


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        response.headers.setdefault(
            "Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"
        )
        return response


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: Callable[..., object], max_bytes: int) -> None:
        super().__init__(app)  # type: ignore[arg-type]
        self.max_bytes = max_bytes

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > self.max_bytes:
            return problem_response(
                status=413,
                error_type="payload_too_large",
                title="Request payload too large",
                detail=f"Request bodies are limited to {self.max_bytes} bytes.",
            )
        return await call_next(request)
