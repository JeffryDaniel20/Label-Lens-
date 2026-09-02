"""Unit tests for P5-T3's retry/timeout/DLQ policy as wired into
`run_analysis_stage`. Same fake-pool, no-live-Redis approach as
`test_analysis_worker.py` (P5-T2)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest
from arq.worker import Retry

from app.analysis import dlq, service
from app.analysis.models import AnalysisState, DeadLetterReason
from app.analysis.retry_policy import PermanentStageError, TransientStageError
from app.analysis.stages import STAGE_FUNCTIONS
from app.analysis.worker import WorkerSettings, reap_stalled_analyses_job, run_analysis_stage
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


class TestTransientRetryThenExhaustion:
    def test_a_transient_failure_is_retried_via_arq_retry(self, db, monkeypatch) -> None:
        org, analysis = _make_analysis(db)
        monkeypatch.setitem(
            STAGE_FUNCTIONS,
            AnalysisState.VALIDATING,
            lambda db, analysis: (_ for _ in ()).throw(TransientStageError("provider outage")),
        )
        # queued -> validating (no stage fn on `queued`).
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "1"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)
        assert analysis.state is AnalysisState.VALIDATING

        pool = _FakePool()
        with pytest.raises(Retry) as excinfo:
            asyncio.run(
                run_analysis_stage(
                    {"redis": pool, "job_id": "2", "job_try": 1}, str(analysis.id), str(org.id)
                )
            )
        assert excinfo.value.defer_score is not None
        assert excinfo.value.defer_score > 0
        assert pool.calls == []  # nothing chained - Arq itself owns the retry
        db.refresh(analysis)
        assert analysis.state is AnalysisState.VALIDATING  # unchanged, not failed yet
        assert dlq.list_dead_letters(db, organization_id=org.id) == []

    def test_exhausting_retries_dead_letters_as_still_retryable(self, db, monkeypatch) -> None:
        org, analysis = _make_analysis(db)
        monkeypatch.setitem(
            STAGE_FUNCTIONS,
            AnalysisState.VALIDATING,
            lambda db, analysis: (_ for _ in ()).throw(TransientStageError("provider outage")),
        )
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "1"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)

        # Attempt number == MAX_STAGE_ATTEMPTS (3): no more retries left.
        pool = _FakePool()
        asyncio.run(
            run_analysis_stage(
                {"redis": pool, "job_id": "2", "job_try": 3}, str(analysis.id), str(org.id)
            )
        )
        db.refresh(analysis)

        assert analysis.state is AnalysisState.FAILED
        assert analysis.failure_stage == "validating"
        assert analysis.retryable is True
        assert pool.calls == []

        records = dlq.list_dead_letters(db, organization_id=org.id)
        assert len(records) == 1
        assert records[0].reason is DeadLetterReason.STAGE_EXHAUSTED
        assert records[0].stage == "validating"
        assert records[0].attempt_count == 3
        assert "provider outage" in records[0].error_message


class TestPermanentFailure:
    def test_a_permanent_failure_is_dead_lettered_immediately_no_retry(
        self, db, monkeypatch
    ) -> None:
        org, analysis = _make_analysis(db)
        monkeypatch.setitem(
            STAGE_FUNCTIONS,
            AnalysisState.VALIDATING,
            lambda db, analysis: (_ for _ in ()).throw(PermanentStageError("corrupt input")),
        )
        asyncio.run(
            run_analysis_stage({"redis": _FakePool(), "job_id": "1"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)

        pool = _FakePool()
        # Even on the very first attempt, a permanent error never retries.
        asyncio.run(
            run_analysis_stage(
                {"redis": pool, "job_id": "2", "job_try": 1}, str(analysis.id), str(org.id)
            )
        )
        db.refresh(analysis)

        assert analysis.state is AnalysisState.FAILED
        assert analysis.retryable is False
        assert pool.calls == []

        records = dlq.list_dead_letters(db, organization_id=org.id)
        assert len(records) == 1
        assert records[0].reason is DeadLetterReason.PERMANENT_ERROR
        assert records[0].attempt_count == 1


class TestBudgetTimeout:
    def test_an_over_budget_analysis_is_failed_before_its_next_stage_runs(
        self, db, monkeypatch
    ) -> None:
        org, analysis = _make_analysis(db)
        calls: list[str] = []
        monkeypatch.setitem(
            STAGE_FUNCTIONS,
            AnalysisState.VALIDATING,
            lambda db, analysis: calls.append("validating") or None,
        )
        analysis.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=25)
        db.add(analysis)
        db.commit()

        pool = _FakePool()
        asyncio.run(
            run_analysis_stage({"redis": pool, "job_id": "1"}, str(analysis.id), str(org.id))
        )
        db.refresh(analysis)

        assert analysis.state is AnalysisState.FAILED
        assert analysis.failure_stage == "queued"
        assert analysis.retryable is False
        assert calls == []  # the stage function never even ran
        assert pool.calls == []

        records = dlq.list_dead_letters(db, organization_id=org.id)
        assert len(records) == 1
        assert records[0].reason is DeadLetterReason.TIMEOUT


class TestWorkerSettingsCron:
    def test_the_janitor_is_registered_as_a_cron_job(self) -> None:
        assert len(WorkerSettings.cron_jobs) == 1
        cron_job = WorkerSettings.cron_jobs[0]
        assert cron_job.coroutine is reap_stalled_analyses_job
