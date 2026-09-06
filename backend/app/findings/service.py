"""Write findings + `finding_evidence` edges, and read them back (P5-T4).

`persist_findings` is the one function that turns `app.rules.evaluator`'s
pure, in-memory `Finding` objects into real, tenant-scoped, immutable rows -
`rule_eval` itself stays a placeholder (blocked on D-01, no published
ruleset to evaluate against yet), so this is exercised directly against a
real published ruleset + real evaluator output rather than through the full
pipeline, the same scoping this codebase has used for every other task in
this dependency chain (P3-T6, P3-T8) that could not wait on D-01 either.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis
from app.confidence.tiers import compute_analysis_tier
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding, FindingEvidence
from app.platform.errors import NotFound
from app.rules import evaluator
from app.rules.models import RuleRow
from app.rules.schema import Severity

# Statuses whose `evidence_fields` name real, present values that must trace
# to at least one `EvidenceSpan` - `not_applicable`/`insufficient_data` are
# the engine's own explicit "no verdict reached" outcomes and carry a stated
# `reason` instead (see `Finding.details`), never evidence.
_EVIDENCE_BACKED_STATUSES = frozenset({evaluator.FindingStatus.PASS, evaluator.FindingStatus.FAIL})


def persist_findings(
    db: Session,
    *,
    analysis: Analysis,
    extraction: Extraction,
    ruleset_id: uuid.UUID,
    findings: Sequence[evaluator.Finding],
) -> list[Finding]:
    """Persist one `evaluate()` run's output. Raises `RuntimeError` if a
    `pass`/`fail` finding resolves to zero evidence spans across all of its
    `evidence_fields` - this should be structurally impossible (P3-T6 already
    rewrites any field that failed verification to an explicit absence
    before `rule_eval` ever runs, which the engine's own `insufficient_data`
    check catches upstream of this function), so a violation here means that
    invariant broke down somewhere and must not be silently persisted as if
    the finding were properly evidenced.
    """
    tier_result = compute_analysis_tier(db, extraction=extraction)
    field_confidence_by_path = {f.field_path: f.confidence for f in tier_result.fields}

    rows: list[Finding] = []
    for finding in findings:
        rule_row = db.scalar(
            select(RuleRow).where(
                RuleRow.ruleset_id == ruleset_id, RuleRow.rule_key == finding.rule_key
            )
        )
        if rule_row is None:
            raise ValueError(
                f"No published rule row for {finding.rule_key!r} in ruleset {ruleset_id} - "
                "the evaluator must have been run against a different ruleset than the one "
                "given here."
            )

        row = Finding(
            organization_id=analysis.organization_id,
            analysis_id=analysis.id,
            ruleset_id=ruleset_id,
            rule_id=rule_row.id,
            rule_key=finding.rule_key,
            rule_version=finding.rule_version,
            status=finding.status,
            severity=finding.severity,
            message=finding.message,
            details={"reason": finding.reason, "evidence_fields": list(finding.evidence_fields)},
            confidence=_finding_confidence(finding, field_confidence_by_path),
        )
        db.add(row)
        db.flush()
        rows.append(row)

        if finding.status in _EVIDENCE_BACKED_STATUSES:
            _attach_evidence(
                db,
                organization_id=analysis.organization_id,
                finding=row,
                extraction_id=extraction.id,
                field_paths=finding.evidence_fields,
            )

    return rows


def _finding_confidence(
    finding: evaluator.Finding, field_confidence_by_path: dict[str, float]
) -> float:
    if finding.status not in _EVIDENCE_BACKED_STATUSES:
        return 0.0
    if not finding.evidence_fields:
        return 1.0
    return min(field_confidence_by_path.get(path, 0.0) for path in finding.evidence_fields)


def _attach_evidence(
    db: Session,
    *,
    organization_id: uuid.UUID,
    finding: Finding,
    extraction_id: uuid.UUID,
    field_paths: Sequence[str],
) -> None:
    span_count = 0
    for path in field_paths:
        field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction_id,
                ExtractedField.field_path == path,
            )
        )
        if field is None:
            continue
        spans = db.scalars(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == field.id)
        ).all()
        for span in spans:
            db.add(
                FindingEvidence(
                    organization_id=organization_id,
                    finding_id=finding.id,
                    extracted_field_id=field.id,
                    evidence_span_id=span.id,
                )
            )
            span_count += 1
    db.flush()

    if span_count == 0:
        raise RuntimeError(
            f"Finding {finding.rule_key!r} (status={finding.status!r}) resolved to zero "
            "evidence spans across all of its evidence fields - a pass/fail finding must "
            "always trace to at least one verified field's evidence."
        )


def list_findings(
    db: Session,
    *,
    organization_id: uuid.UUID,
    analysis_id: uuid.UUID,
    status: evaluator.FindingStatus | None = None,
    severity: Severity | None = None,
) -> list[Finding]:
    """IMPLEMENTATION.md §7: `GET /v1/analyses/{id}/findings` - "filter by
    status/severity"."""
    stmt = select(Finding).where(
        Finding.organization_id == organization_id, Finding.analysis_id == analysis_id
    )
    if status is not None:
        stmt = stmt.where(Finding.status == status)
    if severity is not None:
        stmt = stmt.where(Finding.severity == severity)
    stmt = stmt.order_by(Finding.created_at)
    return list(db.scalars(stmt).all())


def get_finding(db: Session, *, organization_id: uuid.UUID, finding_id: uuid.UUID) -> Finding:
    finding = db.scalar(
        select(Finding).where(
            Finding.id == finding_id, Finding.organization_id == organization_id
        )
    )
    if finding is None:
        # Mirrors the 404-not-403 convention used everywhere else in this
        # codebase: a caller outside this tenant must not learn the finding
        # exists at all.
        raise NotFound("Finding not found.")
    return finding


def get_finding_evidence(
    db: Session, *, organization_id: uuid.UUID, finding_id: uuid.UUID
) -> list[FindingEvidence]:
    """IMPLEMENTATION.md §7: `GET /v1/findings/{id}/evidence` - the rows a
    caller resolves into page/bbox/snippet/signed-URL detail; joining in the
    actual `EvidenceSpan`/`FilePage` rows is the router's job, since building
    a signed download URL needs the storage client and settings this
    (DB-only) module deliberately has no access to."""
    get_finding(db, organization_id=organization_id, finding_id=finding_id)
    stmt = (
        select(FindingEvidence)
        .where(
            FindingEvidence.finding_id == finding_id,
            FindingEvidence.organization_id == organization_id,
        )
        .order_by(FindingEvidence.created_at)
    )
    return list(db.scalars(stmt).all())
