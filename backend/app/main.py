"""FastAPI application factory."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from typing import Any

from arq.connections import ArqRedis
from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.analysis.router import router as analysis_router
from app.analysis.worker import build_arq_pool
from app.catalog.router import router as catalog_router
from app.db import models as _models  # noqa: F401  (registers metadata)
from app.db.session import get_engine, init_engine
from app.findings.router import router as findings_router
from app.identity.router import router as identity_router
from app.identity.sessions import (
    MemorySessionStore,
    RedisSessionStore,
    SessionManager,
    SessionStore,
)
from app.ingestion.av import build_av_scanner
from app.ingestion.router import router as ingestion_router
from app.platform.config import Settings, get_settings
from app.platform.errors import install_error_handlers
from app.platform.logging import configure_logging, get_logger
from app.platform.middleware import (
    BodySizeLimitMiddleware,
    CorrelationIdMiddleware,
    SecurityHeadersMiddleware,
)
from app.platform.ratelimit import MemoryCounterStore, RateLimiter, RedisCounterStore
from app.reports.router import router as reports_router
from app.storage.client import build_storage_client
from app.storage.router import router as storage_router

_log = get_logger("app")

health_router = APIRouter(tags=["ops"])


@health_router.get("/healthz")
def healthz() -> dict[str, str]:
    """Liveness: the process is up. Never touches dependencies."""
    return {"status": "ok"}


@health_router.get("/readyz")
def readyz() -> dict[str, Any]:
    """Readiness: report each dependency separately so a partial outage is visible."""
    checks: dict[str, str] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["database"] = "ok"
    except Exception as exc:  # pragma: no cover - exercised via failure injection
        checks["database"] = f"error: {type(exc).__name__}"
    ready = all(v == "ok" for v in checks.values())
    return {"status": "ready" if ready else "degraded", "checks": checks}


def _build_stores(settings: Settings) -> tuple[SessionStore, Any]:
    """Redis in real environments, in-memory for local/test runs without Redis."""
    if settings.environment in ("local", "test") and settings.redis_url.startswith("memory://"):
        return MemorySessionStore(), MemoryCounterStore()
    try:
        import redis

        client = redis.Redis.from_url(settings.redis_url, decode_responses=True)
        client.ping()
        return RedisSessionStore(client), RedisCounterStore(client)
    except Exception as exc:
        if settings.is_production:
            raise
        _log.warning("redis_unavailable_using_memory", error=str(exc))
        return MemorySessionStore(), MemoryCounterStore()


async def _build_arq_pool_or_none(settings: Settings) -> ArqRedis | None:
    """A real Arq pool if a real Redis is configured; `None` otherwise.

    Mirrors `_build_stores`'s own graceful-degradation shape exactly: the
    `memory://` placeholder (the default in local/test, set in
    `tests/conftest.py`) is never attempted as a real connection, and any
    other connection failure is swallowed outside production. Without this,
    every existing test that submits an analysis over HTTP - and there are
    many, none of which run a live Redis - would otherwise hang for several
    seconds retrying a connection before failing the whole request.
    Submission still creates the `Analysis` row and returns 201/200 either
    way; only the "start working on it automatically" part is skipped.
    """
    if settings.environment in ("local", "test") and settings.redis_url.startswith("memory://"):
        return None
    try:
        return await build_arq_pool(settings)
    except Exception as exc:
        if settings.is_production:
            raise
        _log.warning("arq_pool_unavailable_analyses_will_not_auto_start", error=str(exc))
        return None


def _build_lifespan(
    settings: Settings,
) -> Callable[[FastAPI], AbstractAsyncContextManager[None]]:
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.arq_pool = await _build_arq_pool_or_none(settings)
        try:
            yield
        finally:
            if app.state.arq_pool is not None:
                await app.state.arq_pool.close()

    return lifespan


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(
        json_output=settings.environment != "local",
        level="DEBUG" if settings.debug else "INFO",
    )
    init_engine(settings.database_url, echo=False)

    app = FastAPI(
        title="LabelLens API",
        version="0.1.0",
        docs_url=None if settings.is_production else "/docs",
        openapi_url=None if settings.is_production else "/openapi.json",
        lifespan=_build_lifespan(settings),
    )
    app.state.settings = settings
    app.state.arq_pool = None  # set for real once the lifespan startup runs

    session_store, counter_store = _build_stores(settings)
    app.state.session_manager = SessionManager(
        session_store,
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
    )
    app.state.rate_limiter = RateLimiter(counter_store)
    app.state.storage_client = build_storage_client(settings)
    app.state.av_scanner = build_av_scanner(host=settings.clamd_host, port=settings.clamd_port)

    app.add_middleware(BodySizeLimitMiddleware, max_bytes=settings.max_request_bytes)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "X-CSRF-Token", "X-Correlation-Id", "Authorization"],
    )

    install_error_handlers(app)
    app.include_router(health_router)
    app.include_router(identity_router)
    app.include_router(catalog_router)
    app.include_router(storage_router)
    app.include_router(ingestion_router)
    app.include_router(analysis_router)
    app.include_router(findings_router)
    app.include_router(reports_router)
    return app
