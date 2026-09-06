"""Integration tests for the findings HTTP endpoints (P5-T4), over the real
router: auth, tenancy, status/severity filtering, and the evidence-detail
endpoint's signed page URL. Findings are inserted directly via
`app.findings.service.persist_findings` (see that module's own test suite)
rather than through a real `rule_eval` run, which stays a placeholder.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

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

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


@pytest.fixture
def analysis_with_finding(owner: ApiSession, db):
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
    analysis = Analysis(
        organization_id=org_id, product_version_id=version.id, state=AnalysisState.RULE_EVAL,
        idempotency_key=uuid.uuid4().hex,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org_id, analysis_id=analysis.id, schema_version="1.0.0", payload={},
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
        severity=Severity.MAJOR.value, effective_from=dt.date(2024, 1, 1), payload={},
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
                status=FindingStatus.FAIL, message="Net quantity is missing units.",
                reason=None, evidence_fields=("quantity.net_quantity",),
            )
        ],
    )
    db.commit()
    return analysis, findings[0]


class TestListFindings:
    def test_requires_authentication(self, client) -> None:
        response = client.get(f"/v1/analyses/{uuid.uuid4()}/findings")
        assert response.status_code == 401

    def test_a_foreign_analysis_is_not_found(self, owner: ApiSession) -> None:
        response = owner.get(f"/v1/analyses/{uuid.uuid4()}/findings")
        assert response.status_code == 404

    def test_lists_findings_with_embedded_evidence_refs(
        self, owner: ApiSession, analysis_with_finding
    ) -> None:
        analysis, finding = analysis_with_finding
        response = owner.get(f"/v1/analyses/{analysis.id}/findings")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 1
        assert body[0]["rule_key"] == "IN-TEST-RULE"
        assert body[0]["status"] == "fail"
        assert len(body[0]["evidence_refs"]) == 1

    def test_filters_by_status(self, owner: ApiSession, analysis_with_finding) -> None:
        analysis, _finding = analysis_with_finding
        response = owner.get(
            f"/v1/analyses/{analysis.id}/findings", params={"status": "pass"}
        )
        assert response.status_code == 200
        assert response.json() == []


class TestFindingEvidence:
    def test_returns_page_bbox_snippet_and_a_signed_url(
        self, owner: ApiSession, analysis_with_finding
    ) -> None:
        _analysis, finding = analysis_with_finding
        response = owner.get(f"/v1/findings/{finding.id}/evidence")
        assert response.status_code == 200, response.text
        body = response.json()
        assert len(body) == 1
        detail = body[0]
        assert detail["field_path"] == "quantity.net_quantity"
        assert detail["bbox"] == [1.0, 2.0, 3.0, 4.0]
        assert detail["text_snippet"] == "250 g"
        assert detail["page_image_url"]
        assert detail["page_image_expires_in"] > 0

    def test_a_foreign_findings_evidence_is_not_found(self, owner: ApiSession) -> None:
        response = owner.get(f"/v1/findings/{uuid.uuid4()}/evidence")
        assert response.status_code == 404
