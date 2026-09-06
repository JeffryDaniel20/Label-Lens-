"""Integration tests for the report HTTP endpoints (P7-T1): auth, tenancy,
and generating + reading back a real snapshot over the real router.
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

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


def _png_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(180, 180, 180)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


@pytest.fixture
def analysis_with_finding(owner: ApiSession, client, db):
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
        organization_id=org_id, product_version_id=version.id, state=AnalysisState.NEEDS_REVIEW,
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
    client.app.state.storage_client = _FakeStorageClient({page.render_key: _png_bytes()})
    persist_findings(
        db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
        findings=[
            RuleFinding(
                rule_key="IN-TEST-RULE", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message="ok", reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ],
    )
    db.commit()
    return analysis


class TestGenerateReport:
    def test_requires_authentication(self, client) -> None:
        response = client.post(f"/v1/analyses/{uuid.uuid4()}/reports")
        assert response.status_code == 401

    def test_a_foreign_analysis_is_not_found(self, owner: ApiSession) -> None:
        response = owner.post(f"/v1/analyses/{uuid.uuid4()}/reports")
        assert response.status_code == 404

    def test_generates_a_report_with_the_expected_shape(
        self, owner: ApiSession, analysis_with_finding
    ) -> None:
        response = owner.post(f"/v1/analyses/{analysis_with_finding.id}/reports")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "json"
        assert body["analysis_id"] == str(analysis_with_finding.id)
        assert body["sha256"]
        assert body["snapshot"]["verdict_summary"]["overall_status"] == "pass"
        assert len(body["snapshot"]["findings"]) == 1
        assert body["snapshot"]["findings"][0]["evidence"][0]["image_base64"]


class TestGetReport:
    def test_a_foreign_report_is_not_found(self, owner: ApiSession) -> None:
        response = owner.get(f"/v1/reports/{uuid.uuid4()}")
        assert response.status_code == 404

    def test_reads_back_a_generated_report(
        self, owner: ApiSession, analysis_with_finding
    ) -> None:
        generated = owner.post(f"/v1/analyses/{analysis_with_finding.id}/reports").json()
        response = owner.get(f"/v1/reports/{generated['id']}")
        assert response.status_code == 200, response.text
        assert response.json()["sha256"] == generated["sha256"]
