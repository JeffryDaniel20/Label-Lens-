"""The app-lifecycle-managed Arq pool (`app.main._build_arq_pool_or_none`):
graceful degradation when no real Redis is configured, mirroring
`_build_stores`'s own shape exactly."""

from __future__ import annotations

import asyncio

import pytest

from app.main import _build_arq_pool_or_none
from app.platform.config import Settings

pytestmark = pytest.mark.unit


def _settings(**overrides) -> Settings:
    base = {"secret_key": "x" * 40, "database_url": "sqlite://"}
    base.update(overrides)
    return Settings(**base)


class TestBuildArqPoolOrNone:
    def test_the_memory_placeholder_never_attempts_a_real_connection(self) -> None:
        settings = _settings(environment="local", redis_url="memory://")
        pool = asyncio.run(_build_arq_pool_or_none(settings))
        assert pool is None

    def test_test_environment_with_memory_also_skips(self) -> None:
        settings = _settings(environment="test", redis_url="memory://")
        pool = asyncio.run(_build_arq_pool_or_none(settings))
        assert pool is None

    def test_an_unreachable_real_redis_degrades_gracefully_outside_production(
        self, monkeypatch
    ) -> None:
        settings = _settings(
            environment="local", redis_url="redis://this-host-does-not-exist:6379/0"
        )

        async def _raise(_settings):
            raise ConnectionError("could not connect")

        monkeypatch.setattr("app.main.build_arq_pool", _raise)
        pool = asyncio.run(_build_arq_pool_or_none(settings))
        assert pool is None

    def test_production_re_raises_a_connection_failure(self, monkeypatch) -> None:
        settings = _settings(
            environment="production",
            redis_url="redis://this-host-does-not-exist:6379/0",
            cookie_secure=True,
        )

        async def _raise(_settings):
            raise ConnectionError("could not connect")

        monkeypatch.setattr("app.main.build_arq_pool", _raise)
        with pytest.raises(ConnectionError):
            asyncio.run(_build_arq_pool_or_none(settings))

    def test_a_reachable_real_redis_returns_a_real_pool(self, monkeypatch) -> None:
        settings = _settings(environment="local", redis_url="redis://somehost:6379/0")
        sentinel = object()

        async def _fake_build(_settings):
            return sentinel

        monkeypatch.setattr("app.main.build_arq_pool", _fake_build)
        pool = asyncio.run(_build_arq_pool_or_none(settings))
        assert pool is sentinel
