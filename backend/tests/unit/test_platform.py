"""Unit tests for configuration, logging redaction, rate limiting and sessions."""

from __future__ import annotations

import time

import pytest
from pydantic import ValidationError

from app.identity.sessions import MemorySessionStore, SessionManager, session_fingerprint
from app.platform.config import Settings
from app.platform.logging import redaction_processor
from app.platform.middleware import _sanitize_correlation_id
from app.platform.ratelimit import MemoryCounterStore, RateLimiter

pytestmark = pytest.mark.unit


class TestConfig:
    def test_missing_required_secret_fails_fast(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LABELLENS_SECRET_KEY", raising=False)
        monkeypatch.setenv("LABELLENS_DATABASE_URL", "sqlite://")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_placeholder_secret_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        with pytest.raises(ValidationError):
            Settings(  # type: ignore[call-arg]
                secret_key="changeme", database_url="sqlite://", _env_file=None
            )

    def test_short_secret_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Settings(secret_key="short", database_url="sqlite://", _env_file=None)  # type: ignore[call-arg]


class TestRedaction:
    def test_sensitive_keys_are_replaced(self) -> None:
        event = {
            "event": "login",
            "password": "hunter2",
            "nested": {"api_key": "llk_abc.def", "email": "a@b.com"},
        }
        out = redaction_processor(None, "info", event)
        assert out["password"] == "[redacted]"
        assert out["nested"]["api_key"] == "[redacted]"
        assert out["nested"]["email"] == "a@b.com"

    def test_bearer_tokens_scrubbed_from_free_text(self) -> None:
        out = redaction_processor(None, "info", {"event": "h", "detail": "Bearer abc123XYZ"})
        assert "abc123XYZ" not in out["detail"]


class TestCorrelationId:
    def test_generated_when_absent_or_hostile(self) -> None:
        assert len(_sanitize_correlation_id(None)) == 32
        assert len(_sanitize_correlation_id("../../etc/passwd")) == 32
        assert len(_sanitize_correlation_id("x" * 200)) == 32

    def test_valid_caller_id_is_preserved(self) -> None:
        assert _sanitize_correlation_id("trace-abc-123") == "trace-abc-123"


class TestRateLimiter:
    def test_blocks_after_limit_and_clears(self) -> None:
        limiter = RateLimiter(MemoryCounterStore())
        for _ in range(3):
            allowed, _ = limiter.hit("k", limit=3, window_seconds=60)
            assert allowed
        allowed, retry_after = limiter.hit("k", limit=3, window_seconds=60)
        assert not allowed and retry_after > 0
        limiter.clear("k")
        assert limiter.hit("k", limit=3, window_seconds=60)[0]

    def test_window_expiry_releases_the_lock(self) -> None:
        limiter = RateLimiter(MemoryCounterStore())
        limiter.hit("k", limit=1, window_seconds=1)
        assert limiter.is_locked("k", limit=1)[0]
        time.sleep(1.1)
        assert not limiter.is_locked("k", limit=1)[0]


class TestSessions:
    def _manager(self, idle: int = 60, absolute: int = 600) -> SessionManager:
        return SessionManager(MemorySessionStore(), idle_seconds=idle, absolute_seconds=absolute)

    def test_create_and_load(self) -> None:
        import uuid

        manager = self._manager()
        sid, data = manager.create(user_id=uuid.uuid4(), org_id=uuid.uuid4())
        assert manager.load(sid) is not None
        assert len(data.csrf_token) > 20

    def test_revocation_is_immediate(self) -> None:
        import uuid

        manager = self._manager()
        sid, _ = manager.create(user_id=uuid.uuid4(), org_id=None)
        manager.revoke(sid)
        assert manager.load(sid) is None

    def test_absolute_expiry_wins_over_idle_refresh(self) -> None:
        import uuid

        manager = self._manager(idle=60, absolute=0)
        sid, _ = manager.create(user_id=uuid.uuid4(), org_id=None)
        assert manager.load(sid) is None

    def test_revoke_all_for_user(self) -> None:
        import uuid

        manager = self._manager()
        user_id = uuid.uuid4()
        manager.create(user_id=user_id, org_id=None)
        manager.create(user_id=user_id, org_id=None)
        assert manager.revoke_all_for_user(user_id) == 2

    def test_fingerprint_is_not_the_session_id(self) -> None:
        assert session_fingerprint("abc") != "abc"
