"""Integration tests for the review-workflow HTTP endpoints (P6-T5): auth,
capability gating, and the request/response contract, over the real router.
Findings/evidence are inserted directly (the same convention
`test_findings_router.py` already uses) rather than through a real
`rule_eval` run - that end-to-end proof lives in `test_review_service.py`,
which exercises the same service functions this router just thinly wraps.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image

from app.analysis.models import Analysis, AnalysisState
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.service import persist_findings
from app.rules.evaluator import Finding as RuleFinding
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Severity
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import ApiSession

pytestmark = pytest.mark.integration


class _FakeStorageClient:
    """The same fake `test_reports_router.py` already uses - report
    generation renders a real evidence crop from the page's stored image,
    which a signoff+report test genuinely exercises (P6-T6), so this file
    needs it too rather than requiring a live MinIO for what is otherwise
    an offline test."""

    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


def _png_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(180, 180, 180)).save(buf, format="PNG")
    return buf.getvalue()


SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


def _make_viewer(owner: ApiSession) -> ApiSession:
    owner.post(
        "/v1/members",
        json={"email": "viewer@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
    )
    return ApiSession(owner.client, "viewer@acmefoods.com", "CorrectHorse42!")


@pytest.fixture
def rig(owner: ApiSession, db):
    """A real, needs-review-state analysis with one real finding and one
    real, evidenced, correctable field (`quantity.net_quantity`)."""
    org_id = uuid.UUID(owner.org_id)
    product = Product(organization_id=org_id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org_id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org_id, product_version_id=version.id, storage_key="k",
        original_filename="f.jpg", sha256="a" * 64, mime="image/jpeg", bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org_id, file_id=file.id, page_no=1, width=10, height=10,
        render_key=f"org/{org_id}/pv/{version.id}/render/a/0001.png",
    )
    db.add(page)
    db.flush()
    owner.client.app.state.storage_client = _FakeStorageClient({page.render_key: _png_bytes()})
    analysis = Analysis(
        organization_id=org_id, product_version_id=version.id,
        state=AnalysisState.NEEDS_REVIEW, idempotency_key=uuid.uuid4().hex,
        category="packaged_food", jurisdictions=["IN"],
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org_id, analysis_id=analysis.id, schema_version="1.0.0",
        payload={
            "schema_version": "1.0.0",
            "ingredients": {"declared_text": {"value": "Sugar", "not_found_reason": None},
                             "items": {"value": None, "not_found_reason": "n/a"}},
            "allergens": {"declaration_text": {"value": None, "not_found_reason": "n/a"},
                          "declared": {"value": None, "not_found_reason": "n/a"}},
            "nutrition": {"serving_size": {"value": None, "not_found_reason": "n/a"},
                          "rows": {"value": None, "not_found_reason": "n/a"}},
            "quantity": {"net_quantity": {"value": "250 g", "not_found_reason": None}},
            "dates": {"manufacture_date": {"value": None, "not_found_reason": "n/a"},
                      "expiry_or_best_before": {"value": None, "not_found_reason": "n/a"},
                      "batch_number": {"value": None, "not_found_reason": "n/a"}},
            "claims": {"items": {"value": None, "not_found_reason": "n/a"}},
            "addresses": {"items": {"value": None, "not_found_reason": "n/a"}},
            "languages": {"detected": {"value": None, "not_found_reason": "n/a"}},
        },
        envelope={}, provider="stub", model="stub", prompt_version="1", prompt_hash="h",
    )
    db.add(extraction)
    db.flush()

    ruleset = Ruleset(
        jurisdiction="IN", category="packaged_food", version="1.0.0",
        effective_from=dt.date(2024, 1, 1), source_citations=["c"], author="test",
        review_date=dt.date(2024, 1, 1), checksum=uuid.uuid4().hex,
    )
    db.add(ruleset)
    db.flush()
    rule_row = RuleRow(
        ruleset_id=ruleset.id, rule_key="IN-TEST-RULE", version=1, title="t", citation="c",
        severity=Severity.MAJOR.value, effective_from=dt.date(2024, 1, 1),
        payload={
            "rule_key": "IN-TEST-RULE",
            "version": 1,
            "title": "Net quantity is declared",
            "citation": "Test citation, not real regulatory content",
            "severity": "major",
            "effective_from": "2024-01-01",
            "effective_to": None,
            "applicability": {
                "jurisdiction": ["IN"],
                "category": ["packaged_food"],
                "predicates": [],
            },
            "logic": {"field_present": "quantity.net_quantity"},
            "requires_fields": ["quantity.net_quantity"],
            "on_missing_fields": "insufficient_data",
            "message": {
                "pass": "Net quantity is declared.",
                "fail": "Net quantity is not declared.",
                "insufficient_data": "Could not verify net quantity.",
            },
            "evidence": {"fields": ["quantity.net_quantity"]},
        },
    )
    db.add(rule_row)
    db.flush()

    result = OcrResult(
        organization_id=org_id, file_page_id=page.id, engine="stub", engine_version="1",
        avg_confidence=0.95, raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org_id, file_page_id=page.id, ocr_result_id=result.id, text="250 g",
        confidence=0.95, x1=0.0, y1=0.0, x2=1.0, y2=1.0, line_no=0,
    )
    db.add(token)
    db.flush()
    field = ExtractedField(
        organization_id=org_id, extraction_id=extraction.id, field_path="quantity.net_quantity",
        value_raw="250 g", confidence=0.95, verified=True, cited_token_ids=[str(token.id)],
    )
    db.add(field)
    db.flush()
    span = EvidenceSpan(
        organization_id=org_id, extracted_field_id=field.id, file_page_id=page.id,
        token_ids=[str(token.id)], x1=1.0, y1=2.0, x2=3.0, y2=4.0, text_snippet="250 g",
    )
    db.add(span)
    db.flush()

    findings = persist_findings(
        db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
        findings=[
            RuleFinding(
                rule_key="IN-TEST-RULE", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message="Net quantity is declared.",
                reason=None, evidence_fields=("quantity.net_quantity",),
            )
        ],
    )
    db.commit()
    return analysis, findings[0]


class TestDecideFinding:
    def test_requires_authentication(self, client) -> None:
        response = client.post(f"/v1/findings/{uuid.uuid4()}/decision", json={"action": "confirm"})
        assert response.status_code == 401

    def test_a_viewer_cannot_decide(self, owner: ApiSession, rig) -> None:
        _analysis, finding = rig
        viewer = _make_viewer(owner)
        response = viewer.post(
            f"/v1/findings/{finding.id}/decision", json={"action": "confirm"}
        )
        assert response.status_code == 403

    def test_confirm_succeeds(self, owner: ApiSession, rig) -> None:
        _analysis, finding = rig
        response = owner.post(
            f"/v1/findings/{finding.id}/decision", json={"action": "confirm"}
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["action"] == "confirm"
        assert body["finding_id"] == str(finding.id)

    def test_override_without_reason_is_400(self, owner: ApiSession, rig) -> None:
        _analysis, finding = rig
        response = owner.post(
            f"/v1/findings/{finding.id}/decision", json={"action": "override"}
        )
        assert response.status_code == 400

    def test_override_with_a_real_reason_succeeds(self, owner: ApiSession, rig) -> None:
        _analysis, finding = rig
        response = owner.post(
            f"/v1/findings/{finding.id}/decision",
            json={"action": "override", "reason": "The label is compliant despite this flag."},
        )
        assert response.status_code == 201, response.text

    def test_a_foreign_finding_is_not_found(self, owner: ApiSession) -> None:
        response = owner.post(
            f"/v1/findings/{uuid.uuid4()}/decision", json={"action": "confirm"}
        )
        assert response.status_code == 404

    def test_list_decisions(self, owner: ApiSession, rig) -> None:
        _analysis, finding = rig
        owner.post(f"/v1/findings/{finding.id}/decision", json={"action": "confirm"})
        response = owner.get(f"/v1/findings/{finding.id}/decisions")
        assert response.status_code == 200
        assert len(response.json()) == 1


class TestCorrectField:
    def test_requires_authentication(self, client) -> None:
        response = client.post(
            f"/v1/analyses/{uuid.uuid4()}/corrections",
            json={"field_path": "quantity.net_quantity", "corrected_value": "260 g"},
        )
        assert response.status_code == 401

    def test_a_viewer_cannot_correct(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        viewer = _make_viewer(owner)
        response = viewer.post(
            f"/v1/analyses/{analysis.id}/corrections",
            json={"field_path": "quantity.net_quantity", "corrected_value": "260 g"},
        )
        assert response.status_code == 403

    def test_a_real_correction_creates_a_child_analysis(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.post(
            f"/v1/analyses/{analysis.id}/corrections",
            json={
                "field_path": "quantity.net_quantity",
                "corrected_value": "260 g",
                "reason": "OCR misread the digit.",
            },
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["correction"]["field_path"] == "quantity.net_quantity"
        assert body["correction"]["corrected_value"] == "260 g"
        assert body["correction"]["original_value"] == "250 g"
        assert body["child_analysis"]["id"] != str(analysis.id)
        assert body["child_analysis"]["state"] in ("completed", "needs_review")

    def test_an_uncorrectable_field_is_400(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.post(
            f"/v1/analyses/{analysis.id}/corrections",
            json={"field_path": "ingredients.items", "corrected_value": "anything"},
        )
        assert response.status_code == 400

    def test_a_foreign_analysis_is_not_found(self, owner: ApiSession) -> None:
        response = owner.post(
            f"/v1/analyses/{uuid.uuid4()}/corrections",
            json={"field_path": "quantity.net_quantity", "corrected_value": "260 g"},
        )
        assert response.status_code == 404


class TestReviewQueue:
    def test_requires_authentication(self, client) -> None:
        response = client.get("/v1/review/queue")
        assert response.status_code == 401

    def test_a_needs_review_analysis_appears_in_the_queue(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.get("/v1/review/queue")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["analysis"]["id"] == str(analysis.id)
        assert "sla_since" in body[0]

    def test_a_viewer_can_see_the_queue(self, owner: ApiSession, rig) -> None:
        viewer = _make_viewer(owner)
        response = viewer.get("/v1/review/queue")
        assert response.status_code == 200


class TestAssignment:
    def test_a_viewer_cannot_assign(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        viewer = _make_viewer(owner)
        response = viewer.patch(
            f"/v1/analyses/{analysis.id}/assignment", json={"reviewer_id": None}
        )
        assert response.status_code == 403

    def test_assigns_and_clears_a_reviewer(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.patch(
            f"/v1/analyses/{analysis.id}/assignment", json={"reviewer_id": owner.user_id}
        )
        assert response.status_code == 200, response.text
        assert response.json()["assigned_reviewer_id"] == owner.user_id

        response = owner.patch(
            f"/v1/analyses/{analysis.id}/assignment", json={"reviewer_id": None}
        )
        assert response.status_code == 200
        assert response.json()["assigned_reviewer_id"] is None


class TestSignoff:
    def test_a_viewer_cannot_sign_off(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        viewer = _make_viewer(owner)
        response = viewer.post(f"/v1/analyses/{analysis.id}/signoff")
        assert response.status_code == 403

    def test_get_signoff_is_null_before_signing_off(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.get(f"/v1/analyses/{analysis.id}/signoff")
        assert response.status_code == 200
        assert response.json() is None

    def test_signs_off_and_then_reports_it(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.post(f"/v1/analyses/{analysis.id}/signoff")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["analysis_id"] == str(analysis.id)
        assert body["finding_set_hash"]

        response = owner.get(f"/v1/analyses/{analysis.id}/signoff")
        assert response.status_code == 200
        assert response.json()["analysis_id"] == str(analysis.id)

    def test_signing_off_twice_is_a_conflict(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        owner.post(f"/v1/analyses/{analysis.id}/signoff")
        response = owner.post(f"/v1/analyses/{analysis.id}/signoff")
        assert response.status_code == 409

    def test_a_signed_off_analysis_rejects_further_decisions(
        self, owner: ApiSession, rig
    ) -> None:
        analysis, finding = rig
        owner.post(f"/v1/analyses/{analysis.id}/signoff")

        response = owner.post(
            f"/v1/findings/{finding.id}/decision", json={"action": "confirm"}
        )
        assert response.status_code == 409

    def test_generating_a_report_after_signoff_marks_it_signed_off(
        self, owner: ApiSession, rig
    ) -> None:
        analysis, _finding = rig
        owner.post(f"/v1/analyses/{analysis.id}/signoff")

        response = owner.post(f"/v1/analyses/{analysis.id}/reports")
        assert response.status_code == 201, response.text
        assert response.json()["signed_off_by"] == owner.user_id

    def test_generating_a_report_before_signoff_is_a_draft(self, owner: ApiSession, rig) -> None:
        analysis, _finding = rig
        response = owner.post(f"/v1/analyses/{analysis.id}/reports")
        assert response.status_code == 201, response.text
        assert response.json()["signed_off_by"] is None
