"""RFC 9457 problem+json error model and FastAPI exception handlers."""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.platform.logging import correlation_id_var, get_logger

PROBLEM_CONTENT_TYPE = "application/problem+json"
_log = get_logger(__name__)


class AppError(Exception):
    """Base class for expected, client-visible failures."""

    status_code = 500
    error_type = "internal_error"
    title = "Internal server error"

    def __init__(self, detail: str | None = None, *, errors: list[dict[str, Any]] | None = None):
        super().__init__(detail or self.title)
        self.detail = detail or self.title
        self.errors = errors or []


class ValidationFailed(AppError):
    status_code = 400
    error_type = "validation_error"
    title = "Request validation failed"


class Unauthenticated(AppError):
    status_code = 401
    error_type = "unauthenticated"
    title = "Authentication required"


class Forbidden(AppError):
    status_code = 403
    error_type = "forbidden"
    title = "You do not have permission to perform this action"


class NotFound(AppError):
    status_code = 404
    error_type = "not_found"
    title = "Resource not found"


class Conflict(AppError):
    status_code = 409
    error_type = "conflict"
    title = "Conflicting state"


class StateInvalid(Conflict):
    error_type = "state_invalid"
    title = "Operation not allowed in the current state"


class PayloadTooLarge(AppError):
    status_code = 413
    error_type = "payload_too_large"
    title = "Request payload too large"


class UnsupportedMediaType(AppError):
    status_code = 415
    error_type = "unsupported_media_type"
    title = "Unsupported media type"


class RateLimited(AppError):
    status_code = 429
    error_type = "rate_limited"
    title = "Too many requests"

    def __init__(self, detail: str | None = None, *, retry_after: int = 60):
        super().__init__(detail)
        self.retry_after = retry_after


class ServiceUnavailable(AppError):
    status_code = 503
    error_type = "service_unavailable"
    title = "A dependency is unavailable"


def problem_response(
    *,
    status: int,
    error_type: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://labellens.dev/errors/{error_type}",
        "title": title,
        "status": status,
        "detail": detail,
        "correlation_id": correlation_id_var.get(),
    }
    if errors:
        body["errors"] = errors
    return JSONResponse(
        status_code=status,
        content=body,
        media_type=PROBLEM_CONTENT_TYPE,
        headers=headers,
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        headers = None
        if isinstance(exc, RateLimited):
            headers = {"Retry-After": str(exc.retry_after)}
        return problem_response(
            status=exc.status_code,
            error_type=exc.error_type,
            title=exc.title,
            detail=exc.detail,
            errors=exc.errors,
            headers=headers,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        errors = [
            {"field": ".".join(str(p) for p in err.get("loc", [])), "message": err.get("msg", "")}
            for err in exc.errors()
        ]
        return problem_response(
            status=400,
            error_type="validation_error",
            title="Request validation failed",
            detail="One or more fields are invalid.",
            errors=errors,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapping = {401: "unauthenticated", 403: "forbidden", 404: "not_found", 405: "not_allowed"}
        return problem_response(
            status=exc.status_code,
            error_type=mapping.get(exc.status_code, "http_error"),
            title=str(exc.detail),
            detail=str(exc.detail),
        )

    @app.exception_handler(Exception)
    async def _unhandled(_request: Request, exc: Exception) -> JSONResponse:
        _log.error("unhandled_exception", exc_info=exc)
        return problem_response(
            status=500,
            error_type="internal_error",
            title="Internal server error",
            detail="An unexpected error occurred. Quote the correlation id when reporting this.",
        )
