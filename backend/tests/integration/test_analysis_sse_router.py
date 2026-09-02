"""Router-level tests for the SSE progress stream (P5-T5): content
negotiation on `GET /v1/analyses/{id}/events` and the real wire format over
an actual `StreamingResponse`.

Every test here targets an **already-terminal** analysis so the stream's
own polling loop closes itself on the very first poll (a terminal state is
a stopping state - see `app.analysis.sse`) - a plain synchronous
`TestClient` request can't drive a background worker to advance a live
one, and the polling/resumption logic itself is already covered without a
real HTTP layer in `tests/unit/test_analysis_sse.py` (via `max_iterations`).
"""

from __future__ import annotations

import json
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
def cancelled_analysis(owner: ApiSession, db) -> str:
    product_id = owner.post(
        "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
    ).json()["id"]
    version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
    _direct_ready_file(
        db, organization_id=uuid.UUID(owner.org_id), version_id=uuid.UUID(version_id)
    )
    analysis_id = owner.post(f"/v1/product-versions/{version_id}/analyses", json={}).json()["id"]
    owner.post(f"/v1/analyses/{analysis_id}/cancel")
    return analysis_id


def _parse_sse_body(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        if not block:
            continue
        data_line = next(line for line in block.splitlines() if line.startswith("data: "))
        events.append(json.loads(data_line[len("data: ") :]))
    return events


class TestSseContentNegotiation:
    def test_the_default_accept_header_returns_json(
        self, owner: ApiSession, cancelled_analysis: str
    ) -> None:
        response = owner.get(f"/v1/analyses/{cancelled_analysis}/events")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("application/json")
        assert isinstance(response.json(), list)

    def test_requesting_event_stream_returns_sse(
        self, owner: ApiSession, cancelled_analysis: str
    ) -> None:
        response = owner.get(
            f"/v1/analyses/{cancelled_analysis}/events",
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")

        events = _parse_sse_body(response.text)
        # "queued" (the initial event) and "cancelled".
        assert [e["to_state"] for e in events] == ["queued", "cancelled"]
        assert all("percentage" in e for e in events)

    def test_last_event_id_resumes_from_the_given_sequence(
        self, owner: ApiSession, cancelled_analysis: str
    ) -> None:
        response = owner.get(
            f"/v1/analyses/{cancelled_analysis}/events",
            headers={"Accept": "text/event-stream", "Last-Event-Id": "1"},
        )
        assert response.status_code == 200
        events = _parse_sse_body(response.text)
        assert [e["to_state"] for e in events] == ["cancelled"]

    def test_a_malformed_last_event_id_is_ignored_not_rejected(
        self, owner: ApiSession, cancelled_analysis: str
    ) -> None:
        response = owner.get(
            f"/v1/analyses/{cancelled_analysis}/events",
            headers={"Accept": "text/event-stream", "Last-Event-Id": "not-a-number"},
        )
        assert response.status_code == 200
        events = _parse_sse_body(response.text)
        # Falls back to since_sequence=0, so both events are still sent.
        assert [e["to_state"] for e in events] == ["queued", "cancelled"]

    def test_requires_authentication(self, client) -> None:
        response = client.get(
            f"/v1/analyses/{uuid.uuid4()}/events", headers={"Accept": "text/event-stream"}
        )
        assert response.status_code == 401

    def test_another_orgs_analysis_is_404_even_over_sse(
        self, client, owner: ApiSession, cancelled_analysis: str
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
        response = other.get(
            f"/v1/analyses/{cancelled_analysis}/events",
            headers={"Accept": "text/event-stream"},
        )
        assert response.status_code == 404


class TestCostFieldsOnAnalysisOut:
    def test_a_fresh_analysis_reports_zero_cost_and_progress(
        self, owner: ApiSession, db
    ) -> None:
        product_id = owner.post(
            "/v1/products", json={"name": "P", "internal_sku": "S1"}
        ).json()["id"]
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        _direct_ready_file(
            db, organization_id=uuid.UUID(owner.org_id), version_id=uuid.UUID(version_id)
        )
        body = owner.post(f"/v1/product-versions/{version_id}/analyses", json={}).json()

        assert body["progress_percentage"] == 0
        assert body["total_tokens_in"] == 0
        assert body["total_tokens_out"] == 0
        assert body["total_cost_cents"] == 0

    def test_recorded_cost_is_visible_through_get_analysis(
        self, owner: ApiSession, db
    ) -> None:
        from app.analysis import costs
        from app.analysis.models import Analysis

        product_id = owner.post(
            "/v1/products", json={"name": "P", "internal_sku": "S1"}
        ).json()["id"]
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        _direct_ready_file(
            db, organization_id=uuid.UUID(owner.org_id), version_id=uuid.UUID(version_id)
        )
        analysis_id = owner.post(
            f"/v1/product-versions/{version_id}/analyses", json={}
        ).json()["id"]

        # Cost accrues *during* a live run, before any terminal state - a
        # terminal `analyses` row can never be UPDATEd again (P5-T1's own
        # guarantee), so this records it the same way a real stage function
        # would while `analysis` is still `queued`/mid-pipeline.
        row = db.get(Analysis, uuid.UUID(analysis_id))
        costs.record_stage_cost(db, row, stage="ocr", provider="google_vision", cost_cents=7)
        db.commit()

        response = owner.get(f"/v1/analyses/{analysis_id}")
        assert response.json()["total_cost_cents"] == 7
