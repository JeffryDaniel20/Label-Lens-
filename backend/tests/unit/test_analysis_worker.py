"""Unit tests for the Arq worker wiring (P5-T2): queue routing and the
chaining/no-chaining decision. No live Redis needed here - `ctx["redis"]`
is a fake pool that records `enqueue_job` calls; the real Arq wiring is
proven separately (against live Redis) in
`tests/integration/test_analysis_worker_live.py`.

No `pytest-asyncio` dependency: `run_analysis_stage` is the only async code
in this codebase, so its handful of tests just drive it directly via
`asyncio.run()` from ordinary sync test functions.
"""

from __future__ import annotations

import asyncio

import pytest
from arq.connections import RedisSettings

from app.analysis import service
from app.analysis.models import AnalysisState
from app.analysis.stages import STAGE_FUNCTIONS
from app.analysis.worker import (
    QUEUE_DEFAULT,
    QUEUE_LLM,
    QUEUE_OCR,
    WorkerSettings,
    _worker_redis_settings_from_env,
    queue_for_state,
    run_analysis_stage,
)
from app.catalog.models import File, FileStatus, Product, ProductVersion
from tests.conftest import make_org

pytestmark = pytest.mark.unit


class _FakePool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, dict]] = []

    async def enqueue_job(self, function: str, *args: object, **kwargs: object) -> None:
        self.calls.append((function, args, kwargs))


def _make_analysis(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    db.add(
        File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key="k",
            original_filename="f.jpg",
            sha256="a" * 64,
            mime="image/jpeg",
            bytes=1,
            status=FileStatus.READY,
        )
    )
    db.flush()
    file_hash = service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)
    analysis, _ = service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return org, analysis


class TestQueueRouting:
    def test_ocr_and_llm_stages_route_to_their_dedicated_queues(self) -> None:
        assert queue_for_state(AnalysisState.OCR) == QUEUE_OCR
        assert queue_for_state(AnalysisState.EXTRACTING) == QUEUE_LLM

    def test_every_other_stage_routes_to_the_default_queue(self) -> None:
        for state in (
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.NORMALIZING,
            AnalysisState.CLASSIFYING,
            AnalysisState.RULE_EVAL,
            AnalysisState.SCORING,
        ):
            assert queue_for_state(state) == QUEUE_DEFAULT


class TestRedisSettingsFallback:
    def test_a_real_dsn_is_used_as_is(self, monkeypatch) -> None:
        monkeypatch.setenv("LABELLENS_REDIS_URL", "redis://myhost:1234/2")
        settings = _worker_redis_settings_from_env()
        assert settings.host == "myhost"
        assert settings.port == 1234
        assert settings.database == 2

    def test_an_unparseable_dsn_like_memory_falls_back_to_the_arq_default(
        self, monkeypatch
    ) -> None:
        monkeypatch.setenv("LABELLENS_REDIS_URL", "memory://")
        settings = _worker_redis_settings_from_env()
        assert settings == RedisSettings()


class TestWorkerSettingsContract:
    def test_registers_exactly_the_one_stage_function(self) -> None:
        assert WorkerSettings.functions == (run_analysis_stage,)

    def test_graceful_shutdown_is_configured(self) -> None:
        assert WorkerSettings.handle_signals is True
        assert WorkerSettings.job_completion_wait > 0

    def test_retries_are_enabled(self) -> None:
        assert WorkerSettings.max_tries > 1


class TestRunAnalysisStageChaining:
    def test_advancing_a_non_stopping_stage_enqueues_the_next_one(self, db, monkeypatch) -> None:
        org, analysis = _make_analysis(db)
        pool = _FakePool()
        ctx = {"redis": pool, "job_id": "job-1"}

        asyncio.run(run_analysis_stage(ctx, str(analysis.id), str(org.id)))

        assert len(pool.calls) == 1
        function, args, kwargs = pool.calls[0]
        assert function == "run_analysis_stage"
        assert args == (str(analysis.id), str(org.id))
        assert kwargs["_queue_name"] == QUEUE_DEFAULT

    def test_reaching_a_stopping_state_does_not_enqueue_anything(self, db, monkeypatch) -> None:
        org, analysis = _make_analysis(db)
        # `extracting` is a real stage now (P3-T5) and would fail here for
        # want of a provider credential. This test is about queue chaining
        # and stopping states, not about extraction, so it is stubbed back
        # to a no-op - extraction has its own suite.
        monkeypatch.setitem(
            STAGE_FUNCTIONS, AnalysisState.EXTRACTING, lambda db, analysis: None
        )
        # 8 calls: queued->validating->preprocessing->ocr->extracting->
        # normalizing->classifying->rule_eval->scoring - drives it all the
        # way to `scoring` so the *next* call reaches `completed`.
        for _ in range(8):
            pool = _FakePool()
            asyncio.run(
                run_analysis_stage(
                    {"redis": pool, "job_id": "x"}, str(analysis.id), str(org.id)
                )
            )
            db.refresh(analysis)
        # `run_analysis_stage` commits through its own, separate DB session
        # (a real worker process's session, not this test's `db` fixture) -
        # `analysis` must be refreshed to see the committed state.
        db.refresh(analysis)
        assert analysis.state is AnalysisState.SCORING

        final_pool = _FakePool()
        asyncio.run(
            run_analysis_stage(
                {"redis": final_pool, "job_id": "last"}, str(analysis.id), str(org.id)
            )
        )
        db.refresh(analysis)
        assert analysis.state is AnalysisState.COMPLETED
        assert final_pool.calls == []  # nothing further was enqueued

    def test_reaching_the_ocr_stage_enqueues_its_job_on_the_ocr_queue(self, db) -> None:
        org, analysis = _make_analysis(db)
        # queued -> validating -> preprocessing, both routed to the default queue.
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "1"}, str(analysis.id), str(org.id))
        )
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "2"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)
        assert analysis.state is AnalysisState.PREPROCESSING

        # preprocessing -> ocr: the *new* state is `ocr`, so the job just
        # enqueued to do ocr's work belongs on the ocr queue.
        pool = _FakePool()
        asyncio.run(
            run_analysis_stage({"redis": pool, "job_id": "3"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)
        assert analysis.state is AnalysisState.OCR
        assert pool.calls[0][2]["_queue_name"] == QUEUE_OCR

    def test_worker_id_is_recorded_on_the_resulting_event(self, db) -> None:
        org, analysis = _make_analysis(db)
        asyncio.run(
            run_analysis_stage(
                {"redis": _FakePool(), "job_id": "job-42"}, str(analysis.id), str(org.id)
            )
        )
        events = service.get_events(db, organization_id=org.id, analysis_id=analysis.id)
        assert events[-1].worker_id == "job-42"

    def test_a_raising_stage_function_propagates_and_does_not_enqueue(
        self, db, monkeypatch
    ) -> None:
        org, analysis = _make_analysis(db)
        monkeypatch.setitem(
            STAGE_FUNCTIONS,
            AnalysisState.VALIDATING,
            lambda db, analysis: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        # First call moves queued -> validating (no stage fn involved yet).
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "1"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)
        assert analysis.state is AnalysisState.VALIDATING

        pool = _FakePool()
        with pytest.raises(RuntimeError):
            asyncio.run(
                run_analysis_stage({"redis": pool, "job_id": "2"}, str(analysis.id), str(org.id))
            )
        assert pool.calls == []
        db.refresh(analysis)
        assert analysis.state is AnalysisState.VALIDATING  # unchanged; rolled back
