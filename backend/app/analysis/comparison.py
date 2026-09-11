"""Version comparison (P6-T7): a side-by-side field and finding diff between
two product versions' analyses, with a "compare under common ruleset" toggle
that isolates *why* a finding's verdict changed - this task's own literal
acceptance criterion, "the view distinguishes label change vs rule change vs
extraction change."

Field diffs come straight from each analysis's latest `ExtractedField` rows
(`value_norm`, falling back to `value_raw`) - a real change on the label, not
inferred. Finding diffs, by default, compare each side's own persisted
`Finding` rows exactly as they were actually judged (each analysis's own
pinned `ruleset_version_id`, per P5-T4) - so with the toggle off, a changed
verdict could stem from either the label or the rules having changed between
the two analyses. With the toggle on, both sides are instead re-evaluated
live (via the same pure `app.rules.evaluator.evaluate` the real pipeline
uses, never a reimplementation) against the SAME ruleset - the newer side's
own pinned one - so any verdict difference left over cannot be attributed to
rule content at all, only to what the label/extraction actually says.

For each changed finding, the cause is attributed in this priority order:
1. `label_change` - any field the finding's own `evidence_fields` depends on
   (the evaluator's own per-finding record of what it actually looked at,
   tighter than a rule's static `requires_fields`) genuinely changed value.
2. `rule_change` - the two sides were judged against different ruleset
   versions and this rule's own content differs between them (compared via
   the same `app.rules.diff` machinery P4-T4 already built for ruleset
   version diffing) - never reachable when the common-ruleset toggle is on,
   since both sides were then judged by identical rule content by
   construction.
3. `extraction_change` - neither of the above: the label's cited fields and
   the rule content are both unchanged, so the verdict differs only because
   something else in the pipeline did (e.g. classification, which field a
   rule's evidence resolved to, or an OCR/extraction quirk) - an honest
   catch-all, not a claim about exactly what changed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis
from app.extraction import facts as facts_schema
from app.extraction.models import ExtractedField, Extraction
from app.extraction.normalize.allergens import ALLERGEN_SYNONYMS
from app.findings.models import Finding
from app.rules import evaluator as rules_evaluator
from app.rules.publish import load_ruleset
from app.rules.schema import Rule

ChangeKind = Literal["added", "removed", "changed", "unchanged"]
FindingCause = Literal["label_change", "rule_change", "extraction_change", "unchanged"]


@dataclass(slots=True, frozen=True)
class FieldDiffEntry:
    field_path: str
    from_value: object | None
    to_value: object | None
    change: ChangeKind


@dataclass(slots=True, frozen=True)
class FindingDiffEntry:
    rule_key: str
    rule_title: str | None
    from_status: str | None
    to_status: str | None
    from_severity: str | None
    to_severity: str | None
    change: ChangeKind
    cause: FindingCause


@dataclass(slots=True, frozen=True)
class ComparisonResult:
    from_analysis_id: uuid.UUID | None
    to_analysis_id: uuid.UUID | None
    from_ruleset_version_id: uuid.UUID | None
    to_ruleset_version_id: uuid.UUID | None
    common_ruleset_applied: bool
    ruleset_changed: bool
    field_diffs: list[FieldDiffEntry]
    finding_diffs: list[FindingDiffEntry]


def _latest_extraction(db: Session, analysis_id: uuid.UUID) -> Extraction | None:
    return db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis_id)
        .order_by(Extraction.created_at.desc())
    )


def _fields_by_path(db: Session, extraction: Extraction | None) -> dict[str, object]:
    if extraction is None:
        return {}
    rows = db.scalars(
        select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
    ).all()
    return {
        row.field_path: (row.value_norm if row.value_norm is not None else row.value_raw)
        for row in rows
    }


def diff_fields(
    from_fields: dict[str, object], to_fields: dict[str, object]
) -> list[FieldDiffEntry]:
    """One entry per distinct field path across both sides, sorted for a
    deterministic, reviewable diff - the same shape `app.rules.diff.diff_rules`
    already established for ruleset diffing."""
    entries: list[FieldDiffEntry] = []
    for path in sorted(from_fields.keys() | to_fields.keys()):
        in_from, in_to = path in from_fields, path in to_fields
        from_value, to_value = from_fields.get(path), to_fields.get(path)
        change: ChangeKind
        if in_from and not in_to:
            change = "removed"
        elif in_to and not in_from:
            change = "added"
        else:
            change = "unchanged" if from_value == to_value else "changed"
        entries.append(FieldDiffEntry(path, from_value, to_value, change))
    return entries


def _persisted_findings(
    db: Session, analysis_id: uuid.UUID
) -> dict[str, rules_evaluator.Finding]:
    rows = db.scalars(select(Finding).where(Finding.analysis_id == analysis_id)).all()
    result: dict[str, rules_evaluator.Finding] = {}
    for row in rows:
        details = row.details or {}
        reason = details.get("reason")
        evidence_fields_raw = details.get("evidence_fields")
        evidence_fields = evidence_fields_raw if isinstance(evidence_fields_raw, list) else []
        result[row.rule_key] = rules_evaluator.Finding(
            rule_key=row.rule_key,
            rule_version=row.rule_version,
            severity=row.severity,
            status=row.status,
            message=row.message,
            reason=str(reason) if reason is not None else None,
            evidence_fields=tuple(str(f) for f in evidence_fields),
        )
    return result


def _recompute_findings(
    db: Session, *, analysis: Analysis, extraction: Extraction | None, ruleset_id: uuid.UUID
) -> tuple[dict[str, rules_evaluator.Finding], dict[str, Rule]]:
    if extraction is None or analysis.category is None or not analysis.jurisdictions:
        return {}, {}
    _ruleset, rules = load_ruleset(db, ruleset_id)
    label_facts = facts_schema.LabelFacts.model_validate(extraction.payload)
    findings = rules_evaluator.evaluate(
        facts=label_facts.model_dump(mode="json"),
        rules=rules,
        as_of=analysis.started_at.date(),
        jurisdiction=analysis.jurisdictions[0],
        category=analysis.category,
        predicate_kwargs={"in_allergen_dictionary": {"dictionary": ALLERGEN_SYNONYMS}},
    )
    return {f.rule_key: f for f in findings}, {r.rule_key: r for r in rules}


def _rules_by_key(db: Session, ruleset_id: uuid.UUID | None) -> dict[str, Rule]:
    if ruleset_id is None:
        return {}
    _ruleset, rules = load_ruleset(db, ruleset_id)
    return {r.rule_key: r for r in rules}


def compare_analyses(
    db: Session,
    *,
    from_analysis: Analysis | None,
    to_analysis: Analysis | None,
    common_ruleset: bool,
) -> ComparisonResult:
    from_extraction = _latest_extraction(db, from_analysis.id) if from_analysis else None
    to_extraction = _latest_extraction(db, to_analysis.id) if to_analysis else None
    from_fields = _fields_by_path(db, from_extraction)
    to_fields = _fields_by_path(db, to_extraction)
    field_diffs = diff_fields(from_fields, to_fields)
    changed_fields = {d.field_path for d in field_diffs if d.change != "unchanged"}

    from_ruleset_id = from_analysis.ruleset_version_id if from_analysis else None
    to_ruleset_id = to_analysis.ruleset_version_id if to_analysis else None
    ruleset_changed = from_ruleset_id != to_ruleset_id

    apply_common = bool(
        common_ruleset and to_ruleset_id is not None and from_analysis and to_analysis
    )
    if apply_common:
        assert from_analysis is not None and to_analysis is not None and to_ruleset_id is not None
        from_findings, _from_rules = _recompute_findings(
            db, analysis=from_analysis, extraction=from_extraction, ruleset_id=to_ruleset_id
        )
        to_findings, to_rules = _recompute_findings(
            db, analysis=to_analysis, extraction=to_extraction, ruleset_id=to_ruleset_id
        )
        title_lookup = to_rules
        from_rule_content = to_rules
        to_rule_content = to_rules
    else:
        from_findings = _persisted_findings(db, from_analysis.id) if from_analysis else {}
        to_findings = _persisted_findings(db, to_analysis.id) if to_analysis else {}
        from_rule_content = _rules_by_key(db, from_ruleset_id)
        to_rule_content = _rules_by_key(db, to_ruleset_id)
        title_lookup = {**from_rule_content, **to_rule_content}

    finding_diffs: list[FindingDiffEntry] = []
    for key in sorted(from_findings.keys() | to_findings.keys()):
        f, t = from_findings.get(key), to_findings.get(key)
        change: ChangeKind
        if f is None:
            change = "added"
        elif t is None:
            change = "removed"
        else:
            change = "unchanged" if f.status == t.status else "changed"

        cause: FindingCause
        if change == "unchanged":
            cause = "unchanged"
        else:
            active = t or f
            assert active is not None
            if set(active.evidence_fields) & changed_fields:
                cause = "label_change"
            elif apply_common:
                # Both sides were judged by identical rule content by
                # construction - a rule-content cause is not even possible.
                cause = "extraction_change"
            else:
                old_rule, new_rule = from_rule_content.get(key), to_rule_content.get(key)
                rule_content_differs = (old_rule is None) != (new_rule is None) or (
                    old_rule is not None
                    and new_rule is not None
                    and old_rule.model_dump() != new_rule.model_dump()
                )
                cause = "rule_change" if rule_content_differs else "extraction_change"

        rule = title_lookup.get(key)
        finding_diffs.append(
            FindingDiffEntry(
                rule_key=key,
                rule_title=rule.title if rule else None,
                from_status=f.status.value if f else None,
                to_status=t.status.value if t else None,
                from_severity=f.severity.value if f else None,
                to_severity=t.severity.value if t else None,
                change=change,
                cause=cause,
            )
        )

    return ComparisonResult(
        from_analysis_id=from_analysis.id if from_analysis else None,
        to_analysis_id=to_analysis.id if to_analysis else None,
        from_ruleset_version_id=from_ruleset_id,
        to_ruleset_version_id=to_ruleset_id,
        common_ruleset_applied=apply_common,
        ruleset_changed=ruleset_changed,
        field_diffs=field_diffs,
        finding_diffs=finding_diffs,
    )
