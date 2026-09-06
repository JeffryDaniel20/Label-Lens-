"""Integration tests for P7-T2's PDF HTTP endpoint: the "no Arq pool
configured" synchronous-render fallback (mirroring `submit_analysis`'s own
fallback) is always exercised over real HTTP; the "queued" async path is
proven separately with a fake pool, since it needs no native rendering
libraries at all - see `test_reports_pdf_service.py` for the real
WeasyPrint-backed render itself, which this router path calls into and is
therefore `weasyprint`-marked/skipped the same way.
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

try:
    import weasyprint  # noqa: F401

    _WEASYPRINT_AVAILABLE = True
except OSError:
    _WEASYPRINT_AVAILABLE = False

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

    def generate_presigned_get(self, key: str, *, expires_in: int) -> str:
        return f"https://fake-storage.test/{key}?expires_in={expires_in}"

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self._objects[key] = data


class _FakeArqPool:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def enqueue_job(self, function: str, *args: object) -> None:
        self.calls.append((function, args))


def _png_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(180, 180, 180)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


@pytest.fixture
def json_report(owner: ApiSession, client, db):
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
                status=FindingStatus.FAIL, message="ok", reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ],
    )
    db.commit()

    generated = owner.post(f"/v1/analyses/{analysis.id}/reports")
    assert generated.status_code == 201, generated.text
    return analysis, generated.json()


class TestRenderReportPdfQueuedPath:
    def test_enqueues_a_job_and_returns_202_when_a_pool_is_configured(
        self, owner: ApiSession, client, json_report
    ) -> None:
        _analysis, report = json_report
        pool = _FakeArqPool()
        client.app.state.arq_pool = pool

        response = owner.post(f"/v1/reports/{report['id']}/pdf")

        assert response.status_code == 202
        assert response.json() == {"queued": True}
        assert pool.calls == [("render_report_pdf_job", (report["id"], owner.org_id))]

        client.app.state.arq_pool = None  # restore for any other test sharing this app


class TestRenderReportPdfSyncPath:
    def test_requires_authentication(self, client) -> None:
        response = client.post(f"/v1/reports/{uuid.uuid4()}/pdf")
        assert response.status_code == 401

    def test_a_foreign_report_is_not_found(self, owner: ApiSession) -> None:
        response = owner.post(f"/v1/reports/{uuid.uuid4()}/pdf")
        assert response.status_code == 404

    @pytest.mark.weasyprint
    @pytest.mark.skipif(
        not _WEASYPRINT_AVAILABLE,
        reason="WeasyPrint's native libraries are not importable in this environment.",
    )
    def test_renders_inline_and_returns_a_signed_download_url(
        self, owner: ApiSession, json_report
    ) -> None:
        _analysis, report = json_report

        response = owner.post(f"/v1/reports/{report['id']}/pdf")
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["kind"] == "pdf"
        assert body["pdf_key"] is not None
        assert body["pdf_download_url"]
        assert body["pdf_download_expires_in"] > 0

        # And the JSON report, read back, is now unaffected - `reports` is
        # append-only, so rendering a PDF never mutates the original row.
        original = owner.get(f"/v1/reports/{report['id']}")
        assert original.json()["kind"] == "json"
        assert original.json()["pdf_key"] is None

        listing = owner.get(f"/v1/analyses/{_analysis.id}/reports")
        assert {r["kind"] for r in listing.json()} == {"json", "pdf"}
