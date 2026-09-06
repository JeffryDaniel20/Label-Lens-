"""Integration tests for P7-T1's DB-touching half: `build_snapshot`
assembling a real snapshot from real rows, and `persist_report` writing it
as an immutable `Report`. Findings/evidence are inserted directly (see
`test_findings_service.py`'s own suite) rather than through a real
`rule_eval` run, which stays a placeholder blocked on D-01.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select

from app.analysis.models import Analysis, AnalysisState, ConfidenceTier
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.service import persist_findings
from app.reports.models import Report
from app.reports.service import build_snapshot, get_report, list_reports, persist_report
from app.rules.evaluator import Finding as RuleFinding
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Severity
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

pytestmark = pytest.mark.integration


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


def _png_bytes(size: tuple[int, int] = (20, 20)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, color=(200, 200, 200)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def rig(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="Masala Chips", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id, product_version_id=version.id, storage_key="k",
        original_filename="label.jpg", sha256="a" * 64, mime="image/jpeg", bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=20, height=20,
        render_key="org/x/render/0001.png",
    )
    db.add(page)
    db.flush()
    analysis = Analysis(
        organization_id=org.id, product_version_id=version.id, state=AnalysisState.NEEDS_REVIEW,
        confidence_tier=ConfidenceTier.MEDIUM, idempotency_key=uuid.uuid4().hex,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org.id, analysis_id=analysis.id, schema_version="1.0.0", payload={},
        envelope={}, provider="gemini", model="gemini-3.8-flash", prompt_version="1",
        prompt_hash="h",
    )
    db.add(extraction)
    db.flush()
    ruleset = Ruleset(
        jurisdiction="IN", category="packaged_food", version="1.0.0",
        effective_from=dt.date(2024, 1, 1), source_citations=["FSSAI reg. X"], author="test",
        review_date=dt.date(2024, 1, 1), checksum=uuid.uuid4().hex,
    )
    db.add(ruleset)
    db.flush()
    analysis.ruleset_version_id = ruleset.id
    rule_row = RuleRow(
        ruleset_id=ruleset.id, rule_key="IN-TEST-RULE", version=1,
        title="Net quantity declared",
        citation="FSSAI (Packaging & Labelling) Regs. 2011, reg. 2.2",
        severity=Severity.MAJOR.value,
        effective_from=dt.date(2024, 1, 1), payload={},
    )
    db.add(rule_row)
    db.flush()

    result = OcrResult(
        organization_id=org.id, file_page_id=page.id, engine="paddleocr", engine_version="3.7",
        avg_confidence=0.95, raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org.id, file_page_id=page.id, ocr_result_id=result.id, text="250 g",
        confidence=0.95, x1=1.0, y1=2.0, x2=10.0, y2=11.0, line_no=0,
    )
    db.add(token)
    db.flush()
    field = ExtractedField(
        organization_id=org.id, extraction_id=extraction.id, field_path="quantity.net_quantity",
        value_raw="250 g", value_norm={"value": 250, "unit": "g"}, confidence=0.95, verified=True,
        cited_token_ids=[str(token.id)],
    )
    db.add(field)
    db.flush()
    span = EvidenceSpan(
        organization_id=org.id, extracted_field_id=field.id, file_page_id=page.id,
        token_ids=[str(token.id)], x1=1.0, y1=2.0, x2=10.0, y2=11.0, text_snippet="250 g",
    )
    db.add(span)
    db.flush()

    persist_findings(
        db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
        findings=[
            RuleFinding(
                rule_key="IN-TEST-RULE", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message="Net quantity is declared.", reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ],
    )
    db.commit()

    storage_client = _FakeStorageClient({page.render_key: _png_bytes()})
    return org, analysis, storage_client


class TestBuildSnapshot:
    def test_the_snapshot_carries_cover_provenance_findings_and_appendix(self, db, rig) -> None:
        _org, analysis, storage_client = rig
        snapshot = build_snapshot(db, analysis=analysis, storage_client=storage_client)

        assert snapshot["cover"]["analysis_id"] == str(analysis.id)
        assert snapshot["verdict_summary"]["overall_status"] == "pass"
        assert snapshot["verdict_summary"]["counts_by_severity"] == {"major": 1}
        assert snapshot["verdict_summary"]["confidence_tier"] == "medium"
        assert snapshot["provenance"]["ruleset"]["jurisdiction"] == "IN"
        assert snapshot["provenance"]["model_manifest"]["extractor_provider"] == "gemini"
        assert "paddleocr@3.7" in snapshot["provenance"]["model_manifest"]["ocr_engines"]
        assert len(snapshot["findings"]) == 1
        finding = snapshot["findings"][0]
        assert finding["citation"].startswith("FSSAI")
        assert finding["evidence"][0]["field_path"] == "quantity.net_quantity"
        assert finding["evidence"][0]["image_base64"]  # a real base64 PNG crop was rendered
        assert len(snapshot["appendix"]["extracted_fields"]) == 1
        assert snapshot["appendix"]["ocr_text_by_page"][0]["text"] == "250 g"
        assert snapshot["not_applicable_rules"] == []
        assert snapshot["insufficient_data"] == []

    def test_an_analysis_with_only_not_applicable_findings_is_insufficient_data(
        self, db, rig
    ) -> None:
        _org, analysis, storage_client = rig
        from app.findings.models import Finding

        db.query(Finding).filter_by(analysis_id=analysis.id).delete()
        rule_row = db.scalar(
            select(RuleRow).where(RuleRow.rule_key == "IN-TEST-RULE")
        )
        persist_findings(
            db, analysis=analysis,
            extraction=db.scalar(select(Extraction).where(Extraction.analysis_id == analysis.id)),
            ruleset_id=rule_row.ruleset_id,
            findings=[
                RuleFinding(
                    rule_key="IN-TEST-RULE", rule_version=1, severity=Severity.MAJOR,
                    status=FindingStatus.NOT_APPLICABLE, message=None,
                    reason="jurisdiction mismatch", evidence_fields=("quantity.net_quantity",),
                )
            ],
        )
        db.commit()

        snapshot = build_snapshot(db, analysis=analysis, storage_client=storage_client)
        assert snapshot["verdict_summary"]["overall_status"] == "insufficient_data"
        assert len(snapshot["not_applicable_rules"]) == 1
        assert snapshot["not_applicable_rules"][0]["reason"] == "jurisdiction mismatch"


class TestPersistReport:
    def test_persists_an_immutable_row_with_a_stable_hash(self, db, rig) -> None:
        _org, analysis, storage_client = rig
        snapshot = build_snapshot(db, analysis=analysis, storage_client=storage_client)

        first = persist_report(db, analysis=analysis, snapshot=snapshot, generated_by=None)
        db.commit()
        second = persist_report(db, analysis=analysis, snapshot=snapshot, generated_by=None)
        db.commit()

        assert first.id != second.id
        assert first.sha256 == second.sha256
        assert first.snapshot["cover"]["report_hash"] == second.snapshot["cover"]["report_hash"]
        assert first.snapshot["cover"]["generated_at"] is not None

        stored = db.scalar(select(Report).where(Report.id == first.id))
        assert stored is not None
        assert stored.kind == "json"

    def test_get_and_list_are_tenant_scoped(self, db, rig) -> None:
        _org, analysis, storage_client = rig
        snapshot = build_snapshot(db, analysis=analysis, storage_client=storage_client)
        report = persist_report(db, analysis=analysis, snapshot=snapshot, generated_by=None)
        db.commit()

        found = get_report(db, organization_id=_org.id, report_id=report.id)
        assert found.id == report.id

        other_org = make_org(db, name="Other Org")
        from app.platform.errors import NotFound

        with pytest.raises(NotFound):
            get_report(db, organization_id=other_org.id, report_id=report.id)

        assert list_reports(db, organization_id=_org.id, analysis_id=analysis.id) == [report]
        assert list_reports(db, organization_id=other_org.id, analysis_id=analysis.id) == []
