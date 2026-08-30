"""Engine and session management, including the per-request tenant context.

Tenant isolation is enforced twice:
  1. application scoping - every tenant query goes through `tenant_scoped()`;
  2. PostgreSQL row-level security - `set_tenant_context()` sets `app.org_id`,
     which the RLS policies created in the migrations compare against.
The second layer is a backstop for a mistake in the first.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import Select, create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None


def create_db_engine(database_url: str, *, echo: bool = False) -> Engine:
    connect_args: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False
    engine = create_engine(
        database_url,
        echo=echo,
        pool_pre_ping=True,
        future=True,
        connect_args=connect_args,
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

    return engine


def init_engine(database_url: str, *, echo: bool = False) -> Engine:
    global _engine, _session_factory
    _engine = create_db_engine(database_url, echo=echo)
    _session_factory = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("Database engine is not initialised; call init_engine() first.")
    return _engine


def get_session_factory() -> sessionmaker[Session]:
    if _session_factory is None:
        raise RuntimeError("Session factory is not initialised; call init_engine() first.")
    return _session_factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def is_postgres(session: Session) -> bool:
    return session.get_bind().dialect.name == "postgresql"


def set_tenant_context(session: Session, org_id: uuid.UUID | None) -> None:
    """Bind the tenant for RLS. No-op on engines without RLS (SQLite in tests)."""
    if not is_postgres(session):
        return
    if org_id is None:
        session.execute(text("SELECT set_config('app.org_id', '', true)"))
    else:
        session.execute(
            text("SELECT set_config('app.org_id', :org_id, true)"), {"org_id": str(org_id)}
        )


def tenant_scoped(stmt: Select[Any], model: Any, org_id: uuid.UUID) -> Select[Any]:
    """Application-level tenant filter. Mandatory for every tenant-owned query."""
    if not hasattr(model, "organization_id"):
        raise TypeError(f"{model.__name__} is not a tenant-scoped model")
    return stmt.where(model.organization_id == org_id)
