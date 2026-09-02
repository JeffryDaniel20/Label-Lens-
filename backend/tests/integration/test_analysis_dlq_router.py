"""HTTP-level tests for the dead-letter endpoints (P5-T3): auth, RBAC,
tenancy, and the list/replay contract. Failures are injected directly at the
DB layer (the same way `test_analysis_dlq.py` does) since no real worker
runs in these tests - only the HTTP surface on top of `app.analysis.dlq` is
under test here.
"""

from __future__ import annotations

import uuid

import pytest

from app.analysis import dlq as dlq_service
from app.analysis.models import AnalysisState, DeadLetterReason
from app.analysis.state_machine import transition
from tests.conftest import ApiSession

pytestmark = pytest.mark.integration

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}


def _direct_ready_file(db, *, organization_id, version_id, sha256: str = "a" * 64) -> None:
    from app.catalog.models import File, FileStatus

    db.add(
        File(
            organization_id=organization_id,
            product_version_id=version_id,
            storage_key=f"org/{organization_id}/pv/{version_id}/x.jpg",
            original_filename="label.jpg",
            sha256=sha256,
            mime="image/jpeg",
            bytes=123,
            status=FileStatus.READY,
        )
    )
    db.commit()


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


@pytest.fixture
def dead_letter(owner: ApiSession, db) -> tuple[str, str]:
    """Submits a real analysis over HTTP, then fails and dead-letters it
    directly at the DB layer (retryable=True). Returns (analysis_id,
    dead_letter_id)."""
    product_id = owner.post(
        "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
    ).json()["id"]
    version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
    _direct_ready_file(
        db, organization_id=uuid.UUID(owner.org_id), version_id=uuid.UUID(version_id)
    )
    analysis_id = owner.post(f"/v1/product-versions/{version_id}/analyses", json={}).json()["id"]

    from app.analysis.models import Analysis

    analysis = db.get(Analysis, uuid.UUID(analysis_id))
    for target in (AnalysisState.VALIDATING, AnalysisState.PREPROCESSING, AnalysisState.OCR):
        transition(db, analysis, target)
    transition(db, analysis, AnalysisState.FAILED, failure_stage="ocr", retryable=True)
    record = dlq_service.record_dead_letter(
        db,
        analysis=analysis,
        stage="ocr",
        reason=DeadLetterReason.STAGE_EXHAUSTED,
        error_message="provider outage",
        attempt_count=3,
    )
    db.commit()
    return analysis_id, str(record.id)


def _make_viewer(owner: ApiSession) -> ApiSession:
    owner.post(
        "/v1/members",
        json={"email": "viewer@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
    )
    return ApiSession(owner.client, "viewer@acmefoods.com", "CorrectHorse42!")


class TestListDeadLetters:
    def test_requires_authentication(self, client) -> None:
        response = client.get("/v1/analyses/dead-letters")
        assert response.status_code == 401

    def test_empty_when_nothing_has_failed(self, owner: ApiSession) -> None:
        response = owner.get("/v1/analyses/dead-letters")
        assert response.status_code == 200
        assert response.json() == []

    def test_lists_a_real_dead_letter(self, owner: ApiSession, dead_letter) -> None:
        analysis_id, dead_letter_id = dead_letter
        response = owner.get("/v1/analyses/dead-letters")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == dead_letter_id
        assert body[0]["analysis_id"] == analysis_id
        assert body[0]["reason"] == "stage_exhausted"
        assert body[0]["replayed_at"] is None

    def test_a_viewer_can_list(self, owner: ApiSession, dead_letter) -> None:
        viewer = _make_viewer(owner)
        response = viewer.get("/v1/analyses/dead-letters")
        assert response.status_code == 200
        assert len(response.json()) == 1

    def test_unreplayed_only_filters_a_replayed_record(
        self, owner: ApiSession, dead_letter
    ) -> None:
        _analysis_id, dead_letter_id = dead_letter
        owner.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")

        response = owner.get("/v1/analyses/dead-letters", params={"unreplayed_only": True})
        assert response.status_code == 200
        assert response.json() == []

    def test_another_orgs_dead_letter_is_not_listed(
        self, client, owner: ApiSession, dead_letter
    ) -> None:
        client.post(
            "/v1/auth/signup",
            json={
                "organization_name": "Beta Foods",
                "email": "owner@betafoods.com",
                "password": "CorrectHorse42!",
            },
        )
        other = ApiSession(client, "owner@betafoods.com", "CorrectHorse42!")
        response = other.get("/v1/analyses/dead-letters")
        assert response.status_code == 200
        assert response.json() == []


class TestReplayDeadLetter:
    def test_requires_authentication(self, client) -> None:
        response = client.post(f"/v1/analyses/dead-letters/{uuid.uuid4()}/replay")
        assert response.status_code == 401

    def test_a_viewer_cannot_replay(self, owner: ApiSession, dead_letter) -> None:
        _analysis_id, dead_letter_id = dead_letter
        viewer = _make_viewer(owner)
        response = viewer.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")
        assert response.status_code == 403

    def test_replay_creates_a_new_queued_analysis(self, owner: ApiSession, dead_letter) -> None:
        analysis_id, dead_letter_id = dead_letter
        response = owner.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] != analysis_id
        assert body["state"] == "queued"

    def test_replaying_twice_is_a_conflict(self, owner: ApiSession, dead_letter) -> None:
        _analysis_id, dead_letter_id = dead_letter
        owner.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")
        response = owner.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")
        assert response.status_code == 400

    def test_replaying_an_unknown_id_is_404(self, owner: ApiSession) -> None:
        response = owner.post(f"/v1/analyses/dead-letters/{uuid.uuid4()}/replay")
        assert response.status_code == 404

    def test_replaying_another_orgs_dead_letter_is_404(
        self, client, owner: ApiSession, dead_letter
    ) -> None:
        _analysis_id, dead_letter_id = dead_letter
        client.post(
            "/v1/auth/signup",
            json={
                "organization_name": "Beta Foods",
                "email": "owner@betafoods.com",
                "password": "CorrectHorse42!",
            },
        )
        other = ApiSession(client, "owner@betafoods.com", "CorrectHorse42!")
        response = other.post(f"/v1/analyses/dead-letters/{dead_letter_id}/replay")
        assert response.status_code == 404
