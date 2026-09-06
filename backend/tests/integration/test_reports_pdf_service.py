"""Integration tests for P7-T2's DB-touching half: `render_and_store_pdf`
actually calling WeasyPrint and writing a new `kind="pdf"` `Report` row.
Marked `weasyprint` and skipped where its native Pango/cairo/gdk-pixbuf
libraries aren't importable - see `tests/unit/test_reports_pdf.py` for the
availability check this mirrors, and `tests/integration/test_reports_service.py`
for the (always-runnable) JSON-snapshot half this task builds on.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.analysis.models import Analysis, AnalysisState
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.service import persist_findings
from app.reports.models import Report
from app.reports.service import build_snapshot, persist_report, render_and_store_pdf
from app.rules.evaluator import Finding as RuleFinding
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Severity
from app.storage.keys import key_belongs_to_org
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

try:
    import weasyprint  # noqa: F401

    _WEASYPRINT_AVAILABLE = True
except OSError:
    _WEASYPRINT_AVAILABLE = False

pytestmark = [
    pytest.mark.integration,
    pytest.mark.weasyprint,
    pytest.mark.skipif(
        not _WEASYPRINT_AVAILABLE,
        reason="WeasyPrint's native libraries are not importable in this environment.",
    ),
]


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes] | None = None) -> None:
        self._objects = objects or {}
        self.put_calls: list[tuple[str, bytes, str]] = []

    def download_object(self, key: str) -> bytes:
        return self._objects[key]

    def put_object(self, key: str, data: bytes, *, content_type: str) -> None:
        self.put_calls.append((key, data, content_type))
        self._objects[key] = data


def _png_bytes(size: tuple[int, int] = (10, 10)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(180, 180, 180)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def rig(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id, product_version_id=version.id, storage_key="k",
        original_filename="f.jpg", sha256="a" * 64, mime="image/jpeg", bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=10, height=10,
        render_key="org/x/render/0001.png",
    )
    db.add(page)
    db.flush()
    analysis = Analysis(
        organization_id=org.id, product_version_id=version.id, state=AnalysisState.NEEDS_REVIEW,
        idempotency_key=uuid.uuid4().hex,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org.id, analysis_id=analysis.id, schema_version="1.0.0", payload={},
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
        organization_id=org.id, file_page_id=page.id, engine="stub", engine_version="1",
        avg_confidence=0.95, raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org.id, file_page_id=page.id, ocr_result_id=result.id, text="250 g",
        confidence=0.95, x1=0.0, y1=0.0, x2=1.0, y2=1.0, line_no=0,
    )
    db.add(token)
    db.flush()
    field = ExtractedField(
        organization_id=org.id, extraction_id=extraction.id, field_path="quantity.net_quantity",
        value_raw="250 g", confidence=0.95, verified=True, cited_token_ids=[str(token.id)],
    )
    db.add(field)
    db.flush()
    span = EvidenceSpan(
        organization_id=org.id, extracted_field_id=field.id, file_page_id=page.id,
        token_ids=[str(token.id)], x1=0.0, y1=0.0, x2=1.0, y2=1.0, text_snippet="250 g",
    )
    db.add(span)
    db.flush()
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

    storage_client = _FakeStorageClient({page.render_key: _png_bytes()})
    snapshot = build_snapshot(db, analysis=analysis, storage_client=storage_client)
    json_report = persist_report(db, analysis=analysis, snapshot=snapshot, generated_by=None)
    db.commit()
    return org, analysis, version, json_report, storage_client


class TestRenderAndStorePdf:
    def test_renders_and_stores_a_real_pdf_as_a_new_row(self, db, rig) -> None:
        org, _analysis, version, json_report, storage_client = rig

        pdf_report = render_and_store_pdf(
            db, json_report=json_report, storage_client=storage_client
        )
        db.commit()

        assert pdf_report.id != json_report.id
        assert pdf_report.kind == "pdf"
        assert pdf_report.analysis_id == json_report.analysis_id
        assert pdf_report.pdf_key is not None
        assert key_belongs_to_org(pdf_report.pdf_key, org.id)
        assert str(version.id) in pdf_report.pdf_key

        stored_bytes = storage_client.put_calls[0][1]
        assert stored_bytes.startswith(b"%PDF-")
        assert pdf_report.sha256 != json_report.sha256  # PDF checksum, not the snapshot hash

        stored_row = db.scalar(select(Report).where(Report.id == pdf_report.id))
        assert stored_row is not None
        assert stored_row.kind == "pdf"
