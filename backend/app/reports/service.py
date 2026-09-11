"""Report snapshot assembler (P7-T1): builds the self-contained JSON
snapshot IMPLEMENTATION.md section 19 describes, and persists it as an
immutable `Report` row.

`build_snapshot` is a read-only query over what the pipeline has already
produced - it invents nothing. Two sections are honestly empty rather than
faked, each documented at its own call site: the reviewer-decision field on
every finding (P6's review workflow doesn't exist yet) and the version
change-log in the appendix (P6-T7's own dedicated comparison view is where a
real diff belongs, not a shallow reimplementation here). Evidence crop
images are real PNG crops, base64-encoded, rendered from the same original
page image `app.storage.service` already downloads elsewhere in this
codebase - no new storage code needed, only a new caller.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis
from app.catalog.models import File, FilePage, Product, ProductVersion
from app.classification.classifier import CLASSIFIER_VERSION
from app.db.base import utcnow
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.extraction.normalize import NORMALIZER_VERSION
from app.findings.models import Finding, FindingEvidence
from app.identity.models import Organization
from app.platform.errors import NotFound
from app.reports.models import Report
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.storage.client import ObjectStorageClient
from app.vision.models import OcrResult, OcrTokenRow

REPORT_SCHEMA_VERSION = "1.0.0"

# Statuses IMPLEMENTATION.md's own findings section groups separately.
_VERDICT_STATUSES = (FindingStatus.PASS, FindingStatus.FAIL)


def build_snapshot(
    db: Session, *, analysis: Analysis, storage_client: ObjectStorageClient
) -> dict[str, Any]:
    version = db.get(ProductVersion, analysis.product_version_id)
    if version is None:
        raise NotFound("Product version not found.")
    product = db.get(Product, version.product_id)
    org = db.get(Organization, analysis.organization_id)
    assert org is not None  # FK integrity, not user input

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    files = list(db.scalars(select(File).where(File.product_version_id == version.id)).all())
    findings = list(
        db.scalars(
            select(Finding).where(Finding.analysis_id == analysis.id).order_by(Finding.created_at)
        ).all()
    )
    ruleset = db.get(Ruleset, analysis.ruleset_version_id) if analysis.ruleset_version_id else None

    return {
        "schema_version": REPORT_SCHEMA_VERSION,
        "cover": {
            "organization_id": str(org.id),
            "organization_name": org.name,
            "product_id": str(product.id) if product else None,
            "product_name": product.name if product else None,
            "product_version_id": str(version.id),
            "analysis_id": str(analysis.id),
            # Filled in by `persist_report` at the moment of actual
            # generation - a pure `build_snapshot` call has no wall clock
            # and no caller identity of its own.
            "generated_at": None,
            "generated_by": None,
            "report_hash": None,
        },
        "verdict_summary": _verdict_summary(analysis, findings),
        "provenance": _provenance(db, extraction, ruleset, files),
        "findings": [
            _finding_entry(db, f, storage_client) for f in findings if f.status in _VERDICT_STATUSES
        ],
        "not_applicable_rules": [
            _finding_entry(db, f, storage_client)
            for f in findings
            if f.status is FindingStatus.NOT_APPLICABLE
        ],
        "insufficient_data": [
            _finding_entry(db, f, storage_client)
            for f in findings
            if f.status is FindingStatus.INSUFFICIENT_DATA
        ],
        "appendix": _appendix(db, extraction, files),
    }


def _verdict_summary(analysis: Analysis, findings: list[Finding]) -> dict[str, Any]:
    countable = (*_VERDICT_STATUSES, FindingStatus.INSUFFICIENT_DATA)
    counted = [f for f in findings if f.status in countable]
    if any(f.status is FindingStatus.FAIL for f in counted):
        overall = "fail"
    elif any(f.status is FindingStatus.INSUFFICIENT_DATA for f in counted):
        overall = "insufficient_data"
    elif counted:
        overall = "pass"
    else:
        # No rule ever reached a real verdict for this analysis (everything
        # was `not_applicable`, or `rule_eval` never ran) - "nothing was
        # checked" is never reported as "pass" (section 1's own principle).
        overall = "insufficient_data"

    severity_counts: dict[str, int] = {}
    for finding in findings:
        if finding.status in _VERDICT_STATUSES:
            key = finding.severity.value
            severity_counts[key] = severity_counts.get(key, 0) + 1

    return {
        "overall_status": overall,
        "counts_by_severity": severity_counts,
        "confidence_tier": analysis.confidence_tier.value if analysis.confidence_tier else None,
        "human_review_status": analysis.state.value,
        # P6's sign-off workflow doesn't exist yet - honestly `None`, not a
        # guessed or default-true value.
        "sign_off": None,
    }


def _provenance(
    db: Session, extraction: Extraction | None, ruleset: Ruleset | None, files: list[File]
) -> dict[str, Any]:
    file_ids = [f.id for f in files]
    pages = (
        list(db.scalars(select(FilePage).where(FilePage.file_id.in_(file_ids))).all())
        if file_ids
        else []
    )
    page_ids = [p.id for p in pages]
    ocr_results = (
        list(db.scalars(select(OcrResult).where(OcrResult.file_page_id.in_(page_ids))).all())
        if page_ids
        else []
    )
    ocr_engines = sorted({f"{r.engine}@{r.engine_version}" for r in ocr_results})

    return {
        "ruleset": (
            {
                "jurisdiction": ruleset.jurisdiction,
                "category": ruleset.category,
                "version": ruleset.version,
                "effective_from": ruleset.effective_from.isoformat(),
            }
            if ruleset is not None
            else None
        ),
        "model_manifest": {
            "ocr_engines": ocr_engines,
            "extractor_provider": extraction.provider if extraction else None,
            "extractor_model": extraction.model if extraction else None,
            "prompt_version": extraction.prompt_version if extraction else None,
            "prompt_hash": extraction.prompt_hash if extraction else None,
            "normalizer_version": NORMALIZER_VERSION,
            "classifier_version": CLASSIFIER_VERSION,
        },
        "file_checksums": [
            {"file_id": str(f.id), "filename": f.original_filename, "sha256": f.sha256}
            for f in files
        ],
    }


def _finding_entry(
    db: Session, finding: Finding, storage_client: ObjectStorageClient
) -> dict[str, Any]:
    rule_row = db.get(RuleRow, finding.rule_id)
    edges = list(
        db.scalars(
            select(FindingEvidence).where(FindingEvidence.finding_id == finding.id)
        ).all()
    )
    evidence = [_evidence_entry(db, edge, storage_client) for edge in edges]

    return {
        "rule_key": finding.rule_key,
        "rule_version": finding.rule_version,
        "citation": rule_row.citation if rule_row else None,
        "severity": finding.severity.value,
        "status": finding.status.value,
        "message": finding.message,
        "reason": finding.details.get("reason"),
        "confidence": finding.confidence,
        "evidence": evidence,
        # P6-T5's review actions (confirm/override/fix-field) don't exist
        # yet - every finding is honestly un-reviewed at generation time.
        "reviewer_decision": None,
    }


def _evidence_entry(
    db: Session, edge: FindingEvidence, storage_client: ObjectStorageClient
) -> dict[str, Any]:
    field = db.get(ExtractedField, edge.extracted_field_id)
    span = db.get(EvidenceSpan, edge.evidence_span_id)
    assert field is not None and span is not None  # noqa: S101 - FK integrity
    page = db.get(FilePage, span.file_page_id)
    assert page is not None  # noqa: S101 - FK integrity

    return {
        "field_path": field.field_path,
        "value_raw": field.value_raw,
        "value_norm": field.value_norm,
        "bbox": [span.x1, span.y1, span.x2, span.y2],
        "text_snippet": span.text_snippet,
        "image_base64": _render_evidence_crop(storage_client, page, span),
    }


def _render_evidence_crop(
    storage_client: ObjectStorageClient, page: FilePage, span: EvidenceSpan
) -> str:
    """A real PNG crop of the cited region, base64-encoded so the snapshot
    stays a single self-contained JSON document rather than a set of live
    image URLs that could later 404 once the original file is purged under
    retention - the whole point of section 19's "self-contained" wording."""
    from PIL import Image

    data = storage_client.download_object(page.render_key)
    image = Image.open(io.BytesIO(data))
    box = (int(span.x1), int(span.y1), int(span.x2), int(span.y2))
    crop = image.crop(box)
    buf = io.BytesIO()
    crop.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _appendix(
    db: Session, extraction: Extraction | None, files: list[File]
) -> dict[str, Any]:
    field_table: list[dict[str, Any]] = []
    if extraction is not None:
        fields = db.scalars(
            select(ExtractedField)
            .where(ExtractedField.extraction_id == extraction.id)
            .order_by(ExtractedField.field_path)
        ).all()
        field_table = [
            {
                "field_path": f.field_path,
                "value_raw": f.value_raw,
                "value_norm": f.value_norm,
                "confidence": f.confidence,
                "verified": f.verified,
            }
            for f in fields
        ]

    file_ids = [f.id for f in files]
    pages = (
        list(
            db.scalars(
                select(FilePage).where(FilePage.file_id.in_(file_ids)).order_by(FilePage.page_no)
            ).all()
        )
        if file_ids
        else []
    )
    ocr_text_by_page = []
    for page in pages:
        tokens = db.scalars(
            select(OcrTokenRow)
            .where(OcrTokenRow.file_page_id == page.id)
            .order_by(OcrTokenRow.line_no)
        ).all()
        ocr_text_by_page.append(
            {
                "file_page_id": str(page.id),
                "page_no": page.page_no,
                "text": " ".join(t.text for t in tokens),
            }
        )

    return {
        "extracted_fields": field_table,
        "ocr_text_by_page": ocr_text_by_page,
        # See the module docstring - a real diff is P6-T7's job.
        "change_log": [],
    }


def compute_snapshot_hash(snapshot: dict[str, Any]) -> str:
    """Stable across regeneration, per the acceptance criterion: hashed over
    everything except `cover.generated_at`/`generated_by`/`report_hash`
    itself, which are metadata about *this particular* generation, not the
    content a byte-stable snapshot is supposed to prove is unchanged."""
    stable = json.loads(json.dumps(snapshot, sort_keys=True, default=str))
    stable["cover"] = {
        k: v
        for k, v in stable["cover"].items()
        if k not in ("generated_at", "generated_by", "report_hash")
    }
    canonical = json.dumps(stable, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def persist_report(
    db: Session,
    *,
    analysis: Analysis,
    snapshot: dict[str, Any],
    generated_by: uuid.UUID | None,
    signed_off_by: uuid.UUID | None = None,
) -> Report:
    """Stamps the volatile cover fields onto an already-built `snapshot`
    (see `build_snapshot`) and writes it as a new, immutable `Report` row -
    never an update to an existing one, matching section 19's "self-
    contained immutable snapshot". `signed_off_by` (P6-T6): passed by the
    caller when a real `ReviewSignoff` exists for `analysis` at generation
    time - IMPLEMENTATION.md §12's "eligible for a final report" made
    concrete as "this specific report row is the final one," not a
    separate `kind` value; a report generated before sign-off leaves this
    `None`, honestly a draft/working report, exactly as this column's own
    docstring already said it could be."""
    snapshot = dict(snapshot)
    snapshot["cover"] = dict(snapshot["cover"])
    snapshot["cover"]["generated_at"] = utcnow().isoformat()
    snapshot["cover"]["generated_by"] = str(generated_by) if generated_by else None
    snapshot["cover"]["report_hash"] = compute_snapshot_hash(snapshot)

    row = Report(
        organization_id=analysis.organization_id,
        analysis_id=analysis.id,
        kind="json",
        snapshot=snapshot,
        sha256=snapshot["cover"]["report_hash"],
        signed_off_by=signed_off_by,
    )
    db.add(row)
    db.flush()
    return row


def get_report(db: Session, *, organization_id: uuid.UUID, report_id: uuid.UUID) -> Report:
    report = db.scalar(
        select(Report).where(Report.id == report_id, Report.organization_id == organization_id)
    )
    if report is None:
        # Mirrors the 404-not-403 convention used everywhere else in this
        # codebase: a caller outside this tenant must not learn it exists.
        raise NotFound("Report not found.")
    return report


def list_reports(
    db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID
) -> list[Report]:
    stmt = (
        select(Report)
        .where(Report.organization_id == organization_id, Report.analysis_id == analysis_id)
        .order_by(Report.generated_at.desc())
    )
    return list(db.scalars(stmt).all())


def render_and_store_pdf(
    db: Session, *, json_report: Report, storage_client: ObjectStorageClient
) -> Report:
    """P7-T2: render the same HTML template `app.reports.pdf` builds from a
    JSON report's own snapshot, store the resulting PDF in object storage,
    and record it as a new, immutable `kind="pdf"` `Report` row - never an
    `UPDATE` to `json_report` itself, since `reports` is append-only.
    `sha256` on a `pdf`-kind row is the checksum of the rendered PDF bytes
    (on a `json`-kind row it's the snapshot hash instead - each `kind`'s
    `sha256` checks the artifact that row actually *is*)."""
    from app.reports.pdf import render_html, render_pdf
    from app.storage.keys import build_report_pdf_key

    analysis = db.get(Analysis, json_report.analysis_id)
    if analysis is None:
        raise NotFound("Analysis not found.")

    html = render_html(json_report.snapshot)
    pdf_bytes = render_pdf(html)
    checksum = hashlib.sha256(pdf_bytes).hexdigest()

    key = build_report_pdf_key(
        organization_id=json_report.organization_id,
        product_version_id=analysis.product_version_id,
        report_id=json_report.id,
    )
    storage_client.put_object(key, pdf_bytes, content_type="application/pdf")

    row = Report(
        organization_id=json_report.organization_id,
        analysis_id=json_report.analysis_id,
        kind="pdf",
        snapshot=json_report.snapshot,
        pdf_key=key,
        sha256=checksum,
    )
    db.add(row)
    db.flush()
    return row
