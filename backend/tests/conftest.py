"""Shared test fixtures.

The fast suite runs against SQLite so it needs no services. Behaviour that only
PostgreSQL can provide (row-level security, the append-only trigger) is covered
by tests marked `postgres`, which run when LABELLENS_TEST_PG_URL is set.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator

import pytest

# Settings are read at import time, so the test environment is set up first.
os.environ.setdefault("LABELLENS_ENVIRONMENT", "test")
os.environ.setdefault("LABELLENS_SECRET_KEY", "test-secret-key-that-is-long-enough-123456")
os.environ.setdefault("LABELLENS_REDIS_URL", "memory://")
# Overridden per test by the `app` fixture; present so unit tests can build Settings.
os.environ.setdefault("LABELLENS_DATABASE_URL", "sqlite://")
os.environ.setdefault("LABELLENS_COOKIE_SECURE", "false")
# Argon2 tuned down for test speed only; production values live in config defaults.
os.environ.setdefault("LABELLENS_ARGON2_MEMORY_KIB", "8192")
os.environ.setdefault("LABELLENS_ARGON2_TIME_COST", "1")

from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy.orm import Session  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import get_session_factory, init_engine  # noqa: E402
from app.identity.models import Membership, MembershipStatus, Organization, Role, User  # noqa: E402
from app.identity.passwords import hash_password  # noqa: E402
from app.main import create_app  # noqa: E402
from app.platform.config import get_settings  # noqa: E402

DEFAULT_PASSWORD = "CorrectHorse42!"


@pytest.fixture
def db_url(tmp_path) -> str:
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def app(db_url: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("LABELLENS_DATABASE_URL", db_url)
    get_settings.cache_clear()
    settings = get_settings()
    engine = init_engine(settings.database_url)
    Base.metadata.create_all(engine)
    application = create_app(settings)
    yield application
    Base.metadata.drop_all(engine)
    get_settings.cache_clear()


@pytest.fixture
def client(app) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def db(app) -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
    finally:
        session.close()


def make_org(db: Session, name: str = "Acme Foods") -> Organization:
    org = Organization(name=name, slug=f"{name.lower().replace(' ', '-')}-{uuid.uuid4().hex[:6]}")
    db.add(org)
    db.flush()
    return org


def make_user(
    db: Session,
    org: Organization,
    role: Role = Role.ANALYST,
    email: str | None = None,
    password: str = DEFAULT_PASSWORD,
) -> User:
    user = User(
        email=email or f"{role.value}-{uuid.uuid4().hex[:8]}@example.com",
        display_name=role.value.title(),
        password_hash=hash_password(password),
    )
    db.add(user)
    db.flush()
    db.add(
        Membership(
            organization_id=org.id,
            user_id=user.id,
            role=role,
            status=MembershipStatus.ACTIVE,
        )
    )
    db.flush()
    return user


class ApiSession:
    """A logged-in HTTP client that carries its session cookie and CSRF token."""

    def __init__(self, client: TestClient, email: str, password: str = DEFAULT_PASSWORD) -> None:
        self.client = client
        response = client.post("/v1/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        body = response.json()
        self.csrf = body["csrf_token"]
        self.user_id = body["user_id"]
        self.org_id = body["org_id"]
        self.cookies = dict(response.cookies)

    def request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers.setdefault("X-CSRF-Token", self.csrf)
        return self.client.request(method, url, headers=headers, cookies=self.cookies, **kwargs)

    def get(self, url: str, **kw):
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw):
        return self.request("POST", url, **kw)

    def patch(self, url: str, **kw):
        return self.request("PATCH", url, **kw)

    def delete(self, url: str, **kw):
        return self.request("DELETE", url, **kw)


@pytest.fixture
def org_factory(db: Session):
    def _factory(name: str = "Acme Foods"):
        org = make_org(db, name)
        db.commit()
        return org

    return _factory


@pytest.fixture
def user_factory(db: Session):
    def _factory(org: Organization, role: Role = Role.ANALYST, email: str | None = None):
        user = make_user(db, org, role, email)
        db.commit()
        return user

    return _factory
