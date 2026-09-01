"""Integration tests for the analysis HTTP endpoints (P5-T1), over the real
router: auth, RBAC, tenancy, idempotent submission, and illegal-transition
rejection at the HTTP layer. Runs against SQLite (the shared `client`/`db`
fixtures) - nothing here is PostgreSQL-specific.
"""

from __future__ import annotations

import uuid

import pytest

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
def version_with_file(owner: ApiSession, db) -> str:
    product_id = owner.post(
        "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
    ).json()["id"]
    version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
    _direct_ready_file(
        db, organization_id=uuid.UUID(owner.org_id), version_id=uuid.UUID(version_id)
    )
    return version_id


class TestSubmitAnalysis:
    def test_requires_authentication(self, client) -> None:
        response = client.post(f"/v1/product-versions/{uuid.uuid4()}/analyses", json={})
        assert response.status_code == 401

    def test_a_version_with_no_files_is_rejected(self, owner: ApiSession) -> None:
        product_id = owner.post(
            "/v1/products", json={"name": "P", "internal_sku": "S1"}
        ).json()["id"]
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        response = owner.post(f"/v1/product-versions/{version_id}/analyses", json={})
        assert response.status_code == 400

    def test_submitting_creates_a_queued_analysis(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        response = owner.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["state"] == "queued"
        assert body["product_version_id"] == version_with_file
        assert body["finished_at"] is None

    def test_resubmitting_returns_the_same_analysis_with_200(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        first = owner.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
        second = owner.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
        assert first.status_code == 201
        assert second.status_code == 200
        assert first.json()["id"] == second.json()["id"]

    def test_submitting_for_another_orgs_version_is_404(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        client.post(
            "/v1/auth/signup",
            json={
                "organization_name": "Beta Foods",
                "email": "owner@betafoods.com",
                "password": "CorrectHorse42!",
            },
        )
        a = ApiSession(client, SIGNUP["email"], SIGNUP["password"])
        b = ApiSession(client, "owner@betafoods.com", "CorrectHorse42!")
        product_id = a.post("/v1/products", json={"name": "P", "internal_sku": "S1"}).json()["id"]
        version_id = a.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        response = b.post(f"/v1/product-versions/{version_id}/analyses", json={})
        assert response.status_code == 404

    def test_viewer_role_cannot_submit_an_analysis(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        owner.post(
            "/v1/members",
            json={
                "email": "viewer@acmefoods.com",
                "role": "viewer",
                "password": "CorrectHorse42!",
            },
        )
        viewer = ApiSession(owner.client, "viewer@acmefoods.com", "CorrectHorse42!")
        response = viewer.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
        assert response.status_code == 403

    def test_submitting_locks_the_product_version(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        owner.post(f"/v1/product-versions/{version_with_file}/analyses", json={})
        response = owner.patch(
            f"/v1/product-versions/{version_with_file}", json={"label": "hijacked"}
        )
        assert response.status_code == 409


class TestGetAnalysisAndEvents:
    def test_get_requires_authentication(self, client) -> None:
        assert client.get(f"/v1/analyses/{uuid.uuid4()}").status_code == 401

    def test_get_an_unknown_analysis_is_404(self, owner: ApiSession) -> None:
        assert owner.get(f"/v1/analyses/{uuid.uuid4()}").status_code == 404

    def test_get_returns_the_submitted_analysis(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        analysis_id = owner.post(
            f"/v1/product-versions/{version_with_file}/analyses", json={}
        ).json()["id"]
        response = owner.get(f"/v1/analyses/{analysis_id}")
        assert response.status_code == 200
        assert response.json()["id"] == analysis_id

    def test_events_lists_the_initial_transition(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        analysis_id = owner.post(
            f"/v1/product-versions/{version_with_file}/analyses", json={}
        ).json()["id"]
        response = owner.get(f"/v1/analyses/{analysis_id}/events")
        assert response.status_code == 200
        events = response.json()
        assert len(events) == 1
        assert events[0] == {
            "sequence": 1,
            "from_state": None,
            "to_state": "queued",
            "occurred_at": events[0]["occurred_at"],
            "reason": None,
        }

    def test_analysis_of_another_org_is_404(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        client.post(
            "/v1/auth/signup",
            json={
                "organization_name": "Beta Foods",
                "email": "owner@betafoods.com",
                "password": "CorrectHorse42!",
            },
        )
        a = ApiSession(client, SIGNUP["email"], SIGNUP["password"])
        b = ApiSession(client, "owner@betafoods.com", "CorrectHorse42!")
        product_id = a.post("/v1/products", json={"name": "P", "internal_sku": "S1"}).json()["id"]
        version_id = a.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        _direct_ready_file(
            db, organization_id=uuid.UUID(a.org_id), version_id=uuid.UUID(version_id)
        )
        analysis_id = a.post(f"/v1/product-versions/{version_id}/analyses", json={}).json()["id"]
        assert b.get(f"/v1/analyses/{analysis_id}").status_code == 404
        assert b.get(f"/v1/analyses/{analysis_id}/events").status_code == 404


class TestCancelAnalysis:
    def test_cancelling_a_queued_analysis_succeeds(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        analysis_id = owner.post(
            f"/v1/product-versions/{version_with_file}/analyses", json={}
        ).json()["id"]
        response = owner.post(f"/v1/analyses/{analysis_id}/cancel")
        assert response.status_code == 200
        assert response.json()["state"] == "cancelled"

    def test_cancelling_an_already_cancelled_analysis_is_a_conflict(
        self, owner: ApiSession, version_with_file: str
    ) -> None:
        analysis_id = owner.post(
            f"/v1/product-versions/{version_with_file}/analyses", json={}
        ).json()["id"]
        owner.post(f"/v1/analyses/{analysis_id}/cancel")
        response = owner.post(f"/v1/analyses/{analysis_id}/cancel")
        assert response.status_code == 409

    def test_viewer_role_cannot_cancel(self, owner: ApiSession, version_with_file: str) -> None:
        analysis_id = owner.post(
            f"/v1/product-versions/{version_with_file}/analyses", json={}
        ).json()["id"]
        owner.post(
            "/v1/members",
            json={
                "email": "viewer@acmefoods.com",
                "role": "viewer",
                "password": "CorrectHorse42!",
            },
        )
        viewer = ApiSession(owner.client, "viewer@acmefoods.com", "CorrectHorse42!")
        response = viewer.post(f"/v1/analyses/{analysis_id}/cancel")
        assert response.status_code == 403
