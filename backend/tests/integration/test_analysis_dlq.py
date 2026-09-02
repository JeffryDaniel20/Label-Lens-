"""Integration tests for the dead-letter queue: recording, listing, and
replay (P5-T3)."""

from __future__ import annotations

import pytest

from app.analysis import dlq, service
from app.analysis.models import AnalysisState, DeadLetterReason
from app.analysis.state_machine import transition
from app.catalog.models import File, FileStatus, Product, ProductVersion
from app.platform.errors import NotFound, ValidationFailed
from tests.conftest import make_org

pytestmark = pytest.mark.integration


def _analysis_with_ready_file(db):
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


def _fail(db, analysis, *, retryable: bool) -> None:
    """Drives a fresh analysis to `failed` the same way the worker does:
    one real transition per stage, then the terminal one."""
    for target in (
        AnalysisState.VALIDATING,
        AnalysisState.PREPROCESSING,
        AnalysisState.OCR,
    ):
        transition(db, analysis, target)
    transition(
        db, analysis, AnalysisState.FAILED, failure_stage="ocr", retryable=retryable
    )
    db.commit()


class TestRecordAndList:
    def test_a_recorded_dead_letter_is_listed_newest_first(self, db) -> None:
        org, analysis = _analysis_with_ready_file(db)
        _fail(db, analysis, retryable=True)
        dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.STAGE_EXHAUSTED,
            error_message="provider outage",
            attempt_count=3,
        )
        db.commit()

        records = dlq.list_dead_letters(db, organization_id=org.id)
        assert len(records) == 1
        assert records[0].analysis_id == analysis.id
        assert records[0].reason is DeadLetterReason.STAGE_EXHAUSTED
        assert records[0].attempt_count == 3
        assert records[0].replayed_at is None

    def test_unreplayed_only_filters_out_replayed_records(self, db) -> None:
        org, analysis = _analysis_with_ready_file(db)
        _fail(db, analysis, retryable=True)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.TIMEOUT,
            error_message="timed out",
            attempt_count=1,
        )
        db.commit()

        dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=record.id)
        db.commit()

        assert dlq.list_dead_letters(db, organization_id=org.id, unreplayed_only=True) == []
        assert len(dlq.list_dead_letters(db, organization_id=org.id)) == 1

    def test_dead_letters_are_tenant_isolated(self, db) -> None:
        org_a, analysis_a = _analysis_with_ready_file(db)
        org_b, _ = _analysis_with_ready_file(db)
        _fail(db, analysis_a, retryable=True)
        dlq.record_dead_letter(
            db,
            analysis=analysis_a,
            stage="ocr",
            reason=DeadLetterReason.TIMEOUT,
            error_message="x",
            attempt_count=1,
        )
        db.commit()

        assert len(dlq.list_dead_letters(db, organization_id=org_a.id)) == 1
        assert dlq.list_dead_letters(db, organization_id=org_b.id) == []


class TestReplay:
    def test_replaying_a_retryable_dead_letter_creates_a_fresh_analysis(self, db) -> None:
        org, analysis = _analysis_with_ready_file(db)
        _fail(db, analysis, retryable=True)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.STAGE_EXHAUSTED,
            error_message="provider outage",
            attempt_count=3,
        )
        db.commit()

        new_analysis = dlq.replay_dead_letter(
            db, organization_id=org.id, dead_letter_id=record.id
        )
        db.commit()

        assert new_analysis.id != analysis.id
        assert new_analysis.state is AnalysisState.QUEUED
        assert new_analysis.product_version_id == analysis.product_version_id
        db.refresh(record)
        assert record.replayed_at is not None
        assert record.replayed_as_analysis_id == new_analysis.id

    def test_replaying_locks_the_product_version(self, db) -> None:
        from app.catalog import service as catalog_service
        from app.platform.errors import StateInvalid

        org, analysis = _analysis_with_ready_file(db)
        version_id = analysis.product_version_id
        _fail(db, analysis, retryable=True)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.TIMEOUT,
            error_message="x",
            attempt_count=1,
        )
        db.commit()

        dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=record.id)
        db.commit()

        version = catalog_service.get_version(db, org_id=org.id, version_id=version_id)
        with pytest.raises(StateInvalid):
            catalog_service.update_version(db, version=version, changes={"version_no": 2})

    def test_a_permanent_failure_cannot_be_replayed(self, db) -> None:
        org, analysis = _analysis_with_ready_file(db)
        _fail(db, analysis, retryable=False)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.PERMANENT_ERROR,
            error_message="corrupt input",
            attempt_count=1,
        )
        db.commit()

        with pytest.raises(ValidationFailed):
            dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=record.id)

    def test_replaying_twice_is_rejected(self, db) -> None:
        org, analysis = _analysis_with_ready_file(db)
        _fail(db, analysis, retryable=True)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis,
            stage="ocr",
            reason=DeadLetterReason.TIMEOUT,
            error_message="x",
            attempt_count=1,
        )
        db.commit()

        dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=record.id)
        db.commit()

        with pytest.raises(ValidationFailed):
            dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=record.id)

    def test_replaying_an_unknown_id_is_not_found(self, db) -> None:
        import uuid

        org, _analysis = _analysis_with_ready_file(db)
        with pytest.raises(NotFound):
            dlq.replay_dead_letter(db, organization_id=org.id, dead_letter_id=uuid.uuid4())

    def test_replaying_another_orgs_dead_letter_is_not_found(self, db) -> None:
        org_a, analysis_a = _analysis_with_ready_file(db)
        org_b, _ = _analysis_with_ready_file(db)
        _fail(db, analysis_a, retryable=True)
        record = dlq.record_dead_letter(
            db,
            analysis=analysis_a,
            stage="ocr",
            reason=DeadLetterReason.TIMEOUT,
            error_message="x",
            attempt_count=1,
        )
        db.commit()

        with pytest.raises(NotFound):
            dlq.replay_dead_letter(db, organization_id=org_b.id, dead_letter_id=record.id)
