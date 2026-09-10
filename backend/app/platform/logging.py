"""Structured JSON logging with correlation-id binding and secret redaction."""

from __future__ import annotations

import logging
import re
import sys
from contextvars import ContextVar
from typing import Any

import structlog

correlation_id_var: ContextVar[str | None] = ContextVar("correlation_id", default=None)
org_id_var: ContextVar[str | None] = ContextVar("org_id", default=None)
actor_id_var: ContextVar[str | None] = ContextVar("actor_id", default=None)

SENSITIVE_KEYS = {
    "password",
    "new_password",
    "current_password",
    "secret",
    "secret_key",
    "token",
    "api_key",
    "authorization",
    "cookie",
    "set-cookie",
    "mfa_secret",
    "totp",
    "recovery_codes",
    "password_hash",
    "dsn",
    "sentry_dsn",
}

REDACTED = "[redacted]"
_BEARER = re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]+")


def _redact_value(value: Any) -> Any:
    if isinstance(value, str):
        return _BEARER.sub("Bearer " + REDACTED, value)
    if isinstance(value, dict):
        return _redact_mapping(value)
    if isinstance(value, list):
        return [_redact_value(v) for v in value]
    return value


def _redact_mapping(data: dict[Any, Any]) -> dict[Any, Any]:
    out: dict[Any, Any] = {}
    for key, value in data.items():
        if isinstance(key, str) and key.lower() in SENSITIVE_KEYS:
            out[key] = REDACTED
        else:
            out[key] = _redact_value(value)
    return out


def redaction_processor(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    return _redact_mapping(dict(event_dict))


def context_processor(
    _logger: Any, _method: str, event_dict: structlog.types.EventDict
) -> structlog.types.EventDict:
    for name, var in (
        ("correlation_id", correlation_id_var),
        ("org_id", org_id_var),
        ("actor_id", actor_id_var),
    ):
        value = var.get()
        if value is not None:
            event_dict.setdefault(name, value)
    return event_dict


def configure_logging(*, json_output: bool = True, level: str = "INFO") -> None:
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level, force=True)
    renderer: Any = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=False)
    )
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            context_processor,
            redaction_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level)),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
