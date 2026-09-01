"""Integration tests for analysis creation, idempotency, and history
(P5-T1), at the service layer. Runs against SQLite (the shared `db`
fixture) - nothing here is PostgreSQL-specific.
"""

from __future__ import annotations

import uuid

import pytest

from app.analysis import service
from app.analysis.models import AnalysisState
from app.analysis.state_machine import transition
from app.catalog import service as catalog_service
from app.catalog.models import File, FileStatus, Product, ProductVersion
from app.platform.errors import NotFound, StateInvalid, ValidationFailed
from tests.conftest import make_org

pytestmark = pytest.mark.integration


def _version_with_ready_file(db, org, *, sha256: str = "a" * 64) -> ProductVersion:
    product = Product(organization_id=org.id, name="P", internal_sku=f"S-{uuid.uuid4().hex[:8]}")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    db.add(
        File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key=f"org/{org.id}/pv/{version.id}/x.jpg",
            original_filename="label.jpg",
            sha256=sha256,
            mime="image/jpeg",
            bytes=123,
            status=FileStatus.READY,
        )
    )
    db.flush()
    return version


class TestFileSetHash:
    def test_a_version_with_no_ready_files_is_rejected(self, db) -> None:
        org = make_org(db)
        product = Product(organization_id=org.id, name="P", internal_sku="S1")
        db.add(product)
        db.flush()
        version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
        db.add(version)
        db.flush()
        with pytest.raises(ValidationFailed):
            service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)

    def test_the_hash_changes_when_the_file_set_changes(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org, sha256="a" * 64)
        hash_one_file = service.compute_file_set_hash(
            db, organization_id=org.id, version_id=version.id
        )
        db.add(
            File(
                organization_id=org.id,
                product_version_id=version.id,
                storage_key=f"org/{org.id}/pv/{version.id}/y.jpg",
                original_filename="label2.jpg",
                sha256="b" * 64,
                mime="image/jpeg",
                bytes=123,
                status=FileStatus.READY,
            )
        )
        db.flush()
        hash_two_files = service.compute_file_set_hash(
            db, organization_id=org.id, version_id=version.id
        )
        assert hash_one_file != hash_two_files

    def test_the_hash_is_stable_for_the_identical_file_set(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        first = service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)
        second = service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)
        assert first == second


class TestCreateOrGetAnalysis:
    def test_creates_a_new_analysis_in_the_queued_state(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        analysis, created = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        assert created is True
        assert analysis.state is AnalysisState.QUEUED
        assert analysis.product_version_id == version.id

    def test_resubmitting_the_identical_request_returns_the_existing_analysis(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        first, created_first = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        second, created_second = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        assert created_first is True
        assert created_second is False
        assert first.id == second.id

    def test_resubmitting_after_the_original_reached_a_terminal_state_still_returns_it(
        self, db
    ) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        analysis, _ = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.FAILED)

        again, created = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        assert created is False
        assert again.id == analysis.id
        assert again.state is AnalysisState.FAILED  # not silently reset

    def test_a_different_file_set_hash_creates_a_genuinely_new_analysis(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        first, _ = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        second, created = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="xyz"
        )
        assert created is True
        assert first.id != second.id

    def test_a_version_from_another_org_is_not_found(self, db) -> None:
        org_a = make_org(db, "Acme")
        org_b = make_org(db, "Beta")
        version = _version_with_ready_file(db, org_a)
        with pytest.raises(NotFound):
            service.create_or_get_analysis(
                db, organization_id=org_b.id, version=version, file_set_hash="abc"
            )

    def test_creating_an_analysis_locks_the_product_version(self, db) -> None:
        """Closes P2-T1's own acceptance criterion: editing a version
        referenced by an analysis is rejected, now that a real analysis can
        exist to reference it."""
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        assert version.is_locked is False

        service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        assert version.is_locked is True

        with pytest.raises(StateInvalid):
            catalog_service.update_version(db, version=version, changes={"label": "hijacked"})


class TestEventsAndHistory:
    def test_events_are_returned_in_sequence_order(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        analysis, _ = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.PREPROCESSING)

        events = service.get_events(db, organization_id=org.id, analysis_id=analysis.id)
        assert [e.sequence for e in events] == [1, 2, 3]
        assert [e.to_state for e in events] == [
            AnalysisState.QUEUED,
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
        ]
        assert events[0].from_state is None
        assert events[1].from_state is AnalysisState.QUEUED

    def test_state_is_reconstructable_purely_from_events(self, db) -> None:
        org = make_org(db)
        version = _version_with_ready_file(db, org)
        analysis, _ = service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash="abc"
        )
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.PREPROCESSING)
        transition(db, analysis, AnalysisState.FAILED)

        events = service.get_events(db, organization_id=org.id, analysis_id=analysis.id)
        reconstructed = service.reconstruct_state(events)
        assert reconstructed is analysis.state is AnalysisState.FAILED

    def test_reconstruct_state_of_no_events_is_none(self) -> None:
        assert service.reconstruct_state([]) is None

    def test_events_from_another_org_are_not_visible(self, db) -> None:
        org_a = make_org(db, "Acme")
        org_b = make_org(db, "Beta")
        version = _version_with_ready_file(db, org_a)
        analysis, _ = service.create_or_get_analysis(
            db, organization_id=org_a.id, version=version, file_set_hash="abc"
        )
        events = service.get_events(db, organization_id=org_b.id, analysis_id=analysis.id)
        assert events == []


class TestGetAnalysis:
    def test_get_analysis_from_another_org_is_not_found(self, db) -> None:
        org_a = make_org(db, "Acme")
        org_b = make_org(db, "Beta")
        version = _version_with_ready_file(db, org_a)
        analysis, _ = service.create_or_get_analysis(
            db, organization_id=org_a.id, version=version, file_set_hash="abc"
        )
        with pytest.raises(NotFound):
            service.get_analysis(db, organization_id=org_b.id, analysis_id=analysis.id)

    def test_get_unknown_analysis_id_is_not_found(self, db) -> None:
        org = make_org(db)
        with pytest.raises(NotFound):
            service.get_analysis(db, organization_id=org.id, analysis_id=uuid.uuid4())
