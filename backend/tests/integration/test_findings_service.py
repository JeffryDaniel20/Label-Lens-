"""Integration tests for P5-T4's persistence half: turning
`app.rules.evaluator.Finding` objects (the pure, in-memory output `rule_eval`
will one day produce for real, once D-01 unblocks it) into real `Finding`/
`FindingEvidence` rows.

Exercised directly against a real published `Ruleset`/`RuleRow` and real
`ExtractedField`/`EvidenceSpan` rows, not through the full pipeline -
`rule_eval` itself is still a placeholder (see `app.analysis.stages`), the
same scoping this dependency chain has used since P3-T6.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.analysis.models import Analysis, AnalysisState
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding, FindingEvidence
from app.findings.service import (
    get_finding,
    get_finding_evidence,
    list_findings,
    persist_findings,
)
from app.platform.errors import NotFound
from app.rules.evaluator import Finding as RuleFinding
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Severity
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

pytestmark = pytest.mark.integration


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
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="k",
        original_filename="f.jpg",
        sha256="a" * 64,
        mime="image/jpeg",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=10, height=10, render_key="r"
    )
    db.add(page)
    db.flush()
    analysis = Analysis(
        organization_id=org.id,
        product_version_id=version.id,
        state=AnalysisState.RULE_EVAL,
        idempotency_key=uuid.uuid4().hex,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org.id,
        analysis_id=analysis.id,
        schema_version="1.0.0",
        payload={},
        envelope={},
        provider="stub",
        model="stub",
        prompt_version="1",
        prompt_hash="h",
    )
    db.add(extraction)
    db.flush()

    ruleset = Ruleset(
        jurisdiction="IN",
        category="packaged_food",
        version="1.0.0",
        effective_from=dt.date(2024, 1, 1),
        source_citations=["FSSAI reg. X"],
        author="test",
        review_date=dt.date(2024, 1, 1),
        checksum=uuid.uuid4().hex,
    )
    db.add(ruleset)
    db.flush()

    return org, page, analysis, extraction, ruleset


def _rule_row(db, ruleset, *, rule_key: str, version: int = 1) -> RuleRow:
    row = RuleRow(
        ruleset_id=ruleset.id,
        rule_key=rule_key,
        version=version,
        title="t",
        citation="c",
        severity=Severity.MAJOR.value,
        effective_from=dt.date(2024, 1, 1),
        payload={},
    )
    db.add(row)
    db.flush()
    return row


def _field_with_evidence(
    db, org, page, extraction, *, field_path: str, value_raw: str = "x"
) -> ExtractedField:
    result = OcrResult(
        organization_id=org.id, file_page_id=page.id, engine="stub", engine_version="1",
        avg_confidence=0.95, raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org.id, file_page_id=page.id, ocr_result_id=result.id, text=value_raw,
        confidence=0.95, x1=0.0, y1=0.0, x2=1.0, y2=1.0, line_no=0,
    )
    db.add(token)
    db.flush()
    field = ExtractedField(
        organization_id=org.id, extraction_id=extraction.id, field_path=field_path,
        value_raw=value_raw, confidence=0.95, verified=True, cited_token_ids=[str(token.id)],
    )
    db.add(field)
    db.flush()
    span = EvidenceSpan(
        organization_id=org.id, extracted_field_id=field.id, file_page_id=page.id,
        token_ids=[str(token.id)], x1=0.0, y1=0.0, x2=1.0, y2=1.0, text_snippet=value_raw,
    )
    db.add(span)
    db.flush()
    return field


class TestPersistFindings:
    def test_a_pass_finding_persists_and_traces_to_its_evidence(self, db, rig) -> None:
        org, page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-TEST-RULE")
        _field_with_evidence(db, org, page, extraction, field_path="quantity.net_quantity")

        findings = [
            RuleFinding(
                rule_key="IN-TEST-RULE", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message="ok", reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ]

        rows = persist_findings(
            db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
            findings=findings,
        )
        db.commit()

        assert len(rows) == 1
        row = rows[0]
        assert row.status is FindingStatus.PASS
        assert row.rule_key == "IN-TEST-RULE"
        assert row.confidence == pytest.approx(0.95)

        edges = db.query(FindingEvidence).filter_by(finding_id=row.id).all()
        assert len(edges) == 1

    def test_a_finding_with_no_published_rule_row_is_rejected(self, db, rig) -> None:
        _org, _page, analysis, extraction, ruleset = rig
        findings = [
            RuleFinding(
                rule_key="NOT-PUBLISHED", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message=None, reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ]
        with pytest.raises(ValueError, match="No published rule row"):
            persist_findings(
                db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
                findings=findings,
            )

    def test_insufficient_data_and_not_applicable_need_no_evidence(self, db, rig) -> None:
        _org, _page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-INSUFFICIENT")
        _rule_row(db, ruleset, rule_key="IN-NA")
        findings = [
            RuleFinding(
                rule_key="IN-INSUFFICIENT", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.INSUFFICIENT_DATA, message="missing",
                reason="missing required field(s)", evidence_fields=("quantity.net_quantity",),
            ),
            RuleFinding(
                rule_key="IN-NA", rule_version=1, severity=Severity.MINOR,
                status=FindingStatus.NOT_APPLICABLE, message=None,
                reason="jurisdiction mismatch", evidence_fields=("quantity.net_quantity",),
            ),
        ]

        rows = persist_findings(
            db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
            findings=findings,
        )
        db.commit()

        assert all(row.confidence == 0.0 for row in rows)
        for row in rows:
            edges = db.query(FindingEvidence).filter_by(finding_id=row.id).all()
            assert edges == []
        insufficient = next(r for r in rows if r.rule_key == "IN-INSUFFICIENT")
        assert insufficient.details["reason"] == "missing required field(s)"

    def test_confidence_is_the_worst_evidence_field_confidence(self, db, rig) -> None:
        org, page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-TWO-FIELDS")
        _field_with_evidence(db, org, page, extraction, field_path="quantity.net_quantity")
        weak_field = _field_with_evidence(
            db, org, page, extraction, field_path="dates.batch_number"
        )
        # Force this second field's own confidence down into the Medium band.
        weak_field.confidence = 0.75
        db.flush()

        findings = [
            RuleFinding(
                rule_key="IN-TWO-FIELDS", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.FAIL, message="bad", reason=None,
                evidence_fields=("quantity.net_quantity", "dates.batch_number"),
            )
        ]
        rows = persist_findings(
            db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
            findings=findings,
        )

        assert rows[0].confidence == pytest.approx(0.75)

    def test_a_pass_finding_that_resolves_to_zero_evidence_spans_raises(self, db, rig) -> None:
        """A structural-invariant guard: this should be unreachable via a
        real pipeline (P3-T6 already demotes any field that failed
        verification before `rule_eval` ever runs), so a field with a value
        but genuinely zero `EvidenceSpan` rows means something upstream
        broke - raise loudly rather than silently persist unbacked evidence."""
        org, _page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-BROKEN")
        unbacked = ExtractedField(
            organization_id=org.id, extraction_id=extraction.id,
            field_path="quantity.net_quantity", value_raw="250 g", confidence=0.9,
            verified=True, cited_token_ids=[],
        )
        db.add(unbacked)
        db.flush()

        findings = [
            RuleFinding(
                rule_key="IN-BROKEN", rule_version=1, severity=Severity.MAJOR,
                status=FindingStatus.PASS, message=None, reason=None,
                evidence_fields=("quantity.net_quantity",),
            )
        ]
        with pytest.raises(RuntimeError, match="zero evidence spans"):
            persist_findings(
                db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
                findings=findings,
            )


class TestListAndGetFindings:
    def test_list_filters_by_status_and_severity(self, db, rig) -> None:
        org, page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-A")
        _rule_row(db, ruleset, rule_key="IN-B")
        _field_with_evidence(db, org, page, extraction, field_path="quantity.net_quantity")
        findings = [
            RuleFinding(
                rule_key="IN-A", rule_version=1, severity=Severity.CRITICAL,
                status=FindingStatus.FAIL, message="m", reason=None,
                evidence_fields=("quantity.net_quantity",),
            ),
            RuleFinding(
                rule_key="IN-B", rule_version=1, severity=Severity.MINOR,
                status=FindingStatus.PASS, message="m", reason=None,
                evidence_fields=("quantity.net_quantity",),
            ),
        ]
        persist_findings(
            db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id,
            findings=findings,
        )
        db.commit()

        all_rows = list_findings(db, organization_id=org.id, analysis_id=analysis.id)
        assert len(all_rows) == 2

        failed = list_findings(
            db, organization_id=org.id, analysis_id=analysis.id, status=FindingStatus.FAIL
        )
        assert [r.rule_key for r in failed] == ["IN-A"]

        critical = list_findings(
            db, organization_id=org.id, analysis_id=analysis.id, severity=Severity.CRITICAL
        )
        assert [r.rule_key for r in critical] == ["IN-A"]

    def test_get_finding_from_another_org_is_not_found(self, db, rig) -> None:
        _org, _page, analysis, extraction, ruleset = rig
        _rule_row(db, ruleset, rule_key="IN-C")
        finding = Finding(
            organization_id=_org.id, analysis_id=analysis.id, ruleset_id=ruleset.id,
            rule_id=db.query(RuleRow).filter_by(rule_key="IN-C").one().id,
            rule_key="IN-C", rule_version=1, status=FindingStatus.NOT_APPLICABLE,
            severity=Severity.MINOR, details={"reason": "x", "evidence_fields": []},
        )
        db.add(finding)
        db.flush()

        other_org = make_org(db, name="Other Org")
        with pytest.raises(NotFound):
            get_finding(db, organization_id=other_org.id, finding_id=finding.id)
        with pytest.raises(NotFound):
            get_finding_evidence(db, organization_id=other_org.id, finding_id=finding.id)
