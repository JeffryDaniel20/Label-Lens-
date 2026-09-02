"""Integration tests for the stalled-analysis reaper (P5-T3)."""

from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from app.analysis import dlq, janitor, service
from app.analysis.models import AnalysisState, DeadLetterReason
from app.analysis.state_machine import transition
from app.catalog.models import File, FileStatus, Product, ProductVersion
from tests.conftest import make_org

pytestmark = pytest.mark.integration


def _analysis(db, *, org=None):
    org = org or make_org(db)
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


def _backdate(db, analysis, minutes: int) -> None:
    analysis.started_at = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes)
    db.add(analysis)
    db.commit()


class TestReapStalledAnalyses:
    def test_a_stalled_automated_stage_is_reaped(self, db) -> None:
        org, analysis = _analysis(db)
        transition(db, analysis, AnalysisState.VALIDATING)
        db.commit()
        _backdate(db, analysis, minutes=25)

        reaped = janitor.reap_stalled_analyses(db)

        assert [a.id for a in reaped] == [analysis.id]
        db.refresh(analysis)
        assert analysis.state is AnalysisState.FAILED
        assert analysis.failure_stage == "validating"
        assert analysis.retryable is True

        records = dlq.list_dead_letters(db, organization_id=org.id)
        assert len(records) == 1
        assert records[0].reason is DeadLetterReason.STALLED
        assert records[0].stage == "validating"

    def test_a_fresh_analysis_within_budget_is_left_alone(self, db) -> None:
        org, analysis = _analysis(db)
        transition(db, analysis, AnalysisState.VALIDATING)
        db.commit()

        reaped = janitor.reap_stalled_analyses(db)

        assert reaped == []
        db.refresh(analysis)
        assert analysis.state is AnalysisState.VALIDATING

    def test_needs_review_is_never_reaped_even_when_very_old(self, db) -> None:
        org, analysis = _analysis(db)
        for target in (
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.OCR,
            AnalysisState.EXTRACTING,
            AnalysisState.NORMALIZING,
            AnalysisState.CLASSIFYING,
            AnalysisState.RULE_EVAL,
            AnalysisState.SCORING,
            AnalysisState.NEEDS_REVIEW,
        ):
            transition(db, analysis, target)
        db.commit()
        _backdate(db, analysis, minutes=999)

        reaped = janitor.reap_stalled_analyses(db)

        assert reaped == []
        db.refresh(analysis)
        assert analysis.state is AnalysisState.NEEDS_REVIEW

    def test_an_already_terminal_analysis_is_never_reaped(self, db) -> None:
        org, analysis = _analysis(db)
        transition(db, analysis, AnalysisState.CANCELLED)
        db.commit()
        _backdate(db, analysis, minutes=999)

        reaped = janitor.reap_stalled_analyses(db)

        assert reaped == []

    def test_reaping_is_cross_organization(self, db) -> None:
        org_a, analysis_a = _analysis(db)
        org_b, analysis_b = _analysis(db)
        transition(db, analysis_a, AnalysisState.VALIDATING)
        transition(db, analysis_b, AnalysisState.VALIDATING)
        transition(db, analysis_b, AnalysisState.PREPROCESSING)
        transition(db, analysis_b, AnalysisState.OCR)
        db.commit()
        _backdate(db, analysis_a, minutes=25)
        _backdate(db, analysis_b, minutes=25)

        reaped = janitor.reap_stalled_analyses(db)

        assert {a.id for a in reaped} == {analysis_a.id, analysis_b.id}

    def test_reaping_twice_is_idempotent(self, db) -> None:
        org, analysis = _analysis(db)
        transition(db, analysis, AnalysisState.VALIDATING)
        db.commit()
        _backdate(db, analysis, minutes=25)

        first = janitor.reap_stalled_analyses(db)
        second = janitor.reap_stalled_analyses(db)

        assert len(first) == 1
        assert second == []  # already terminal now - not a stall candidate any more


class TestReapStalledAnalysesJob:
    def test_the_async_cron_wrapper_runs_and_returns_the_reaped_count(self, db) -> None:
        org, analysis = _analysis(db)
        transition(db, analysis, AnalysisState.VALIDATING)
        db.commit()
        _backdate(db, analysis, minutes=25)

        count = asyncio.run(janitor.reap_stalled_analyses_job({}))

        assert count == 1
        db.refresh(analysis)
        assert analysis.state is AnalysisState.FAILED


class TestReapableStates:
    def test_reapable_states_is_queued_plus_every_pipeline_stage(self) -> None:
        from app.analysis.stages import STAGE_SEQUENCE

        assert janitor.REAPABLE_STATES == frozenset({AnalysisState.QUEUED, *STAGE_SEQUENCE})
