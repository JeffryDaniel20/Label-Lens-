"""Review-workflow actions: deciding on a finding and correcting an
extracted field (P6-T5), plus the review queue and sign-off action that
close the workflow out (P6-T6).

P6-T5 was deliberately scoped to what IMPLEMENTATION.md §12 names for that
task - Confirm, Override, Fix field, Escalate - not Comment (no
acceptance-line test names it) and not sign-off, which is this file's own
`sign_off_analysis` now.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import uuid
from collections.abc import Sequence

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis, AnalysisEvent, AnalysisState
from app.analysis.stages import STOPPING_STATES, advance_analysis
from app.audit.models import ActorType
from app.extraction import facts as facts_schema
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding
from app.platform.errors import Conflict, NotFound, ValidationFailed
from app.review.models import (
    OVERRIDE_REASON_MIN_LENGTH,
    DecisionAction,
    FieldCorrection,
    FindingDecision,
    ReviewSignoff,
)

# The `LabelFacts` (P3-T4) scalar text fields a reviewer can correct today -
# every `Fact[str]` leaf in the schema. The five list-shaped facts
# (`ingredients.items`, `allergens.declared`, `nutrition.rows`,
# `claims.items`, `addresses.items`, `languages.detected`) are deliberately
# excluded: "correct the extracted value" (§12) reads naturally as a single
# wrong scalar, and a structural correction to a list would need its own,
# separately-designed UI/API this task's own acceptance lines don't ask for -
# stated here plainly rather than silently half-supported.
CORRECTABLE_FIELDS: frozenset[str] = frozenset(
    {
        "ingredients.declared_text",
        "allergens.declaration_text",
        "nutrition.serving_size",
        "quantity.net_quantity",
        "dates.manufacture_date",
        "dates.expiry_or_best_before",
        "dates.batch_number",
    }
)

# A correction can only be anchored to a field that already has real,
# verified evidence to carry forward (see `create_field_correction`'s own
# docstring for why) - reachable only from states where `rule_eval` has
# actually run, i.e. every stopping state except an outright pipeline
# failure (which never produced real findings to review to begin with). The
# same set is exactly "the analysis has genuinely stopped and can be
# reviewed at all," so `sign_off_analysis` (P6-T6) reuses it unchanged.
_REVIEWABLE_ANALYSIS_STATES: frozenset[AnalysisState] = frozenset(
    {AnalysisState.NEEDS_REVIEW, AnalysisState.REVIEW, AnalysisState.COMPLETED}
)

# `needs_review`/`review` specifically - an analysis waiting on or actively
# under human attention. `completed` has already finished being reviewed (or
# never needed it), so it does not belong in a queue of open work.
_QUEUE_STATES: frozenset[AnalysisState] = frozenset(
    {AnalysisState.NEEDS_REVIEW, AnalysisState.REVIEW}
)


def _get_signoff(
    db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID
) -> ReviewSignoff | None:
    return db.scalar(
        select(ReviewSignoff).where(
            ReviewSignoff.analysis_id == analysis_id,
            ReviewSignoff.organization_id == organization_id,
        )
    )


def _reject_if_signed_off(
    db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID
) -> None:
    """The "signed-off analyses are read-only" half of P6-T6's acceptance
    criterion, enforced at the one place both `record_finding_decision` and
    `create_field_correction` already have to look an analysis up - a
    signed-off analysis's own findings/fields never change again, so
    neither should any further decision or correction against them."""
    if _get_signoff(db, organization_id=organization_id, analysis_id=analysis_id) is not None:
        raise Conflict("This analysis has already been signed off and is read-only.")


def record_finding_decision(
    db: Session,
    *,
    organization_id: uuid.UUID,
    finding_id: uuid.UUID,
    action: DecisionAction,
    reason: str | None,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
) -> FindingDecision:
    """Append a new decision row - never edits a prior one (see this
    module's own docstring for why). `action=OVERRIDE` requires a real,
    substantive reason (IMPLEMENTATION.md §12's own literal "min 20 chars"),
    checked here rather than at the database, since `confirm`/`escalate`
    correctly allow (and usually have) none at all."""
    finding = db.scalar(
        select(Finding).where(
            Finding.id == finding_id, Finding.organization_id == organization_id
        )
    )
    if finding is None:
        raise NotFound("Finding not found.")
    _reject_if_signed_off(db, organization_id=organization_id, analysis_id=finding.analysis_id)

    if action is DecisionAction.OVERRIDE:
        if reason is None or len(reason.strip()) < OVERRIDE_REASON_MIN_LENGTH:
            raise ValidationFailed(
                f"An override requires a reason of at least {OVERRIDE_REASON_MIN_LENGTH} "
                "characters."
            )

    decision = FindingDecision(
        organization_id=organization_id,
        finding_id=finding.id,
        analysis_id=finding.analysis_id,
        action=action,
        reason=reason,
        actor_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
    )
    db.add(decision)
    db.flush()
    return decision


def list_finding_decisions(
    db: Session, *, organization_id: uuid.UUID, finding_id: uuid.UUID
) -> Sequence[FindingDecision]:
    """Newest first - the same "read the latest, not the only" convention
    `Analysis.state`/`AnalysisEvent` already established."""
    return db.scalars(
        select(FindingDecision)
        .where(
            FindingDecision.finding_id == finding_id,
            FindingDecision.organization_id == organization_id,
        )
        .order_by(FindingDecision.created_at.desc())
    ).all()


def _set_fact_value(payload: dict[str, object], field_path: str, value: str) -> dict[str, object]:
    section_name, leaf_name = field_path.split(".", 1)
    out = copy.deepcopy(payload)
    section = out[section_name]
    assert isinstance(section, dict)  # noqa: S101 - our own field, not user input
    section[leaf_name] = {"value": value, "not_found_reason": None}
    return out


def create_field_correction(
    db: Session,
    *,
    organization_id: uuid.UUID,
    analysis: Analysis,
    field_path: str,
    corrected_value: str,
    reason: str | None,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
) -> tuple[FieldCorrection, Analysis]:
    """Fix-field (IMPLEMENTATION.md §12): corrects one field's value and
    triggers a *rule-only* re-evaluation - no re-OCR, no re-extraction, no
    re-classification, all of which already ran and produced data this
    correction reuses as-is. Returns `(correction, child_analysis)`; the
    child is rule-evaluated and scored synchronously within this call (via
    `advance_analysis`, the same pure per-stage driver the real queue uses -
    no new orchestration code, no separate worker path), so the caller can
    return its finished state immediately rather than polling.

    Only a field with a real, existing, verified `EvidenceSpan` can be
    corrected: the child's own new evidence chain reuses that span's real
    page/bbox/cited-tokens - a genuine "the model read this location on the
    page wrong" correction, not a fabricated citation to a location nothing
    ever pointed at. A field that was never found at all (no prior evidence
    to anchor to) cannot be corrected through this endpoint - stated
    plainly rather than inventing a placeholder bbox for it.
    """
    if field_path not in CORRECTABLE_FIELDS:
        raise ValidationFailed(
            f"{field_path!r} is not a correctable field. Correctable fields: "
            f"{sorted(CORRECTABLE_FIELDS)}."
        )
    if analysis.state not in _REVIEWABLE_ANALYSIS_STATES:
        raise ValidationFailed(
            f"Cannot correct a field on an analysis in state {analysis.state.value!r}."
        )
    _reject_if_signed_off(db, organization_id=organization_id, analysis_id=analysis.id)

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise ValidationFailed("This analysis has no extraction to correct.")

    parent_field = db.scalar(
        select(ExtractedField).where(
            ExtractedField.extraction_id == extraction.id,
            ExtractedField.field_path == field_path,
        )
    )
    if parent_field is None:
        raise ValidationFailed(f"No extracted field found at {field_path!r}.")
    parent_span = db.scalar(
        select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == parent_field.id)
    )
    if parent_span is None:
        raise ValidationFailed(
            f"{field_path!r} has no existing evidence to anchor a correction to."
        )

    original_value = parent_field.value_raw
    corrected_payload = _set_fact_value(extraction.payload, field_path, corrected_value)
    # Validates the whole fact set still round-trips through the real
    # schema, not just this one field in isolation.
    facts_schema.LabelFacts.model_validate(corrected_payload)

    key = f"correction:{analysis.id}:{field_path}:{uuid.uuid4()}"
    child = Analysis(
        organization_id=organization_id,
        product_version_id=analysis.product_version_id,
        state=AnalysisState.RULE_EVAL,
        idempotency_key=key,
        parent_analysis_id=analysis.id,
        category=analysis.category,
        jurisdictions=analysis.jurisdictions,
        category_confidence=analysis.category_confidence,
        jurisdiction_confidence=analysis.jurisdiction_confidence,
        created_by_user_id=actor_id if actor_type is ActorType.USER else None,
    )
    db.add(child)
    db.flush()

    db.add(
        AnalysisEvent(
            analysis_id=child.id,
            organization_id=organization_id,
            sequence=1,
            from_state=None,
            to_state=AnalysisState.RULE_EVAL,
            reason=f"Field correction on {field_path!r} from analysis {analysis.id}.",
        )
    )

    child_extraction = Extraction(
        organization_id=organization_id,
        analysis_id=child.id,
        schema_version=extraction.schema_version,
        payload=corrected_payload,
        envelope=extraction.envelope,
        provider=extraction.provider,
        model=extraction.model,
        prompt_version=extraction.prompt_version,
        prompt_hash=extraction.prompt_hash,
        attempts=extraction.attempts,
        escalated=extraction.escalated,
        tokens_in=0,
        tokens_out=0,
        verified_field_count=extraction.verified_field_count,
        demoted_field_count=extraction.demoted_field_count,
    )
    db.add(child_extraction)
    db.flush()

    parent_fields = db.scalars(
        select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
    ).all()
    parent_spans = {
        span.extracted_field_id: span
        for span in db.scalars(
            select(EvidenceSpan).where(
                EvidenceSpan.extracted_field_id.in_([f.id for f in parent_fields])
            )
        ).all()
    }
    for field in parent_fields:
        is_corrected = field.field_path == field_path
        child_field = ExtractedField(
            organization_id=organization_id,
            extraction_id=child_extraction.id,
            field_path=field.field_path,
            value_raw=corrected_value if is_corrected else field.value_raw,
            not_found_reason=None if is_corrected else field.not_found_reason,
            # A human correction is trusted outright - the same "verified,
            # full confidence" treatment `app.confidence.tiers` already
            # gives any other clean, verified field.
            confidence=1.0 if is_corrected else field.confidence,
            cited_token_ids=field.cited_token_ids,
            value_norm=None if is_corrected else field.value_norm,
            unit=None if is_corrected else field.unit,
            verified=True if is_corrected else field.verified,
            match_ratio=field.match_ratio,
        )
        db.add(child_field)
        db.flush()
        span = parent_spans.get(field.id)
        if span is not None:
            db.add(
                EvidenceSpan(
                    organization_id=organization_id,
                    extracted_field_id=child_field.id,
                    file_page_id=span.file_page_id,
                    token_ids=span.token_ids,
                    x1=span.x1,
                    y1=span.y1,
                    x2=span.x2,
                    y2=span.y2,
                    text_snippet=span.text_snippet,
                    source=span.source,
                )
            )
    db.flush()

    correction = FieldCorrection(
        organization_id=organization_id,
        analysis_id=analysis.id,
        child_analysis_id=child.id,
        field_path=field_path,
        original_value=original_value,
        corrected_value=corrected_value,
        reason=reason,
        actor_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
    )
    db.add(correction)
    db.flush()

    # Rule-only re-evaluation, synchronous: RULE_EVAL -> SCORING -> a
    # stopping state, via the same pure per-stage driver the real queue
    # uses - no separate orchestration path to keep in sync with it.
    while child.state not in STOPPING_STATES:
        advance_analysis(db, child)

    return correction, child


def assign_reviewer(
    db: Session, *, organization_id: uuid.UUID, analysis: Analysis, reviewer_id: uuid.UUID | None
) -> Analysis:
    """Sets (or clears, `reviewer_id=None`) who is working `analysis` -
    a plain, mutable column (`Analysis.assigned_reviewer_id`), safe because
    it can only ever be touched while `state` is non-terminal
    (`reject_terminal_analysis_mutation()`, migration 0006, already enforces
    that at the database level for every column on this row, this one
    included)."""
    analysis.assigned_reviewer_id = reviewer_id
    db.add(analysis)
    db.flush()
    return analysis


def list_review_queue(
    db: Session, *, organization_id: uuid.UUID
) -> list[tuple[Analysis, dt.datetime]]:
    """Every analysis genuinely waiting on or under human review
    (`needs_review`/`review` - `completed` has already been through it, or
    never needed to), each paired with the real timestamp it *first*
    entered that state - the SLA clock IMPLEMENTATION.md §12's queue view
    needs, derived from `AnalysisEvent`'s own append-only history (the same
    "state history is fully reconstructable from events" guarantee P5-T1
    built), not a new column that could drift from what actually happened.
    Oldest-waiting-first, so the queue itself surfaces what needs attention
    soonest."""
    analyses = db.scalars(
        select(Analysis).where(
            Analysis.organization_id == organization_id,
            Analysis.state.in_(_QUEUE_STATES),
        )
    ).all()
    if not analyses:
        return []

    entries: list[tuple[Analysis, dt.datetime]] = []
    for analysis in analyses:
        first_queued_at = db.scalar(
            select(func.min(AnalysisEvent.occurred_at)).where(
                AnalysisEvent.analysis_id == analysis.id,
                AnalysisEvent.to_state.in_(_QUEUE_STATES),
            )
        )
        # Defensive, not expected in practice: an analysis cannot legally
        # reach `needs_review`/`review` without an event recording exactly
        # that transition (`state_machine.transition` writes one atomically
        # with every state change) - fall back to `started_at` rather than
        # crash if this invariant is ever somehow violated.
        entries.append((analysis, first_queued_at or analysis.started_at))
    entries.sort(key=lambda entry: entry[1])
    return entries


def compute_finding_set_hash(findings: Sequence[Finding]) -> str:
    """sha256 over the sorted `(rule_key, rule_version, status)` of every
    finding - IMPLEMENTATION.md §12's own literal "a hash of the finding
    set," independently re-computable later to confirm a signed-off
    analysis's findings genuinely haven't changed since (they can't -
    `findings` is append-only - but the hash makes that a checkable fact,
    not just an architectural promise)."""
    parts = sorted(f"{f.rule_key}:{f.rule_version}:{f.status.value}" for f in findings)
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def sign_off_analysis(
    db: Session,
    *,
    organization_id: uuid.UUID,
    analysis: Analysis,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
) -> ReviewSignoff:
    """Freezes the review (IMPLEMENTATION.md §12): once this returns, no
    further `record_finding_decision`/`create_field_correction` call
    against `analysis` will succeed (`_reject_if_signed_off`) - the literal
    "signed-off analyses are read-only" acceptance criterion. Requires the
    analysis to have actually reached a real reviewable stopping state
    (the same `_REVIEWABLE_ANALYSIS_STATES` `create_field_correction`
    checks) and to not already be signed off (the real, database-enforced
    unique constraint on `ReviewSignoff.analysis_id` is the final backstop
    against a race; this check is the fast, honest-error path for the
    ordinary case)."""
    if analysis.state not in _REVIEWABLE_ANALYSIS_STATES:
        raise ValidationFailed(
            f"Cannot sign off an analysis in state {analysis.state.value!r}."
        )
    if _get_signoff(db, organization_id=organization_id, analysis_id=analysis.id) is not None:
        raise Conflict("This analysis has already been signed off.")

    findings = db.scalars(
        select(Finding).where(Finding.analysis_id == analysis.id)
    ).all()
    signoff = ReviewSignoff(
        organization_id=organization_id,
        analysis_id=analysis.id,
        ruleset_version_id=analysis.ruleset_version_id,
        finding_set_hash=compute_finding_set_hash(findings),
        actor_id=actor_id,
        actor_type=actor_type,
        actor_label=actor_label,
    )
    db.add(signoff)
    db.flush()
    return signoff


def get_signoff(
    db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID
) -> ReviewSignoff | None:
    return _get_signoff(db, organization_id=organization_id, analysis_id=analysis_id)
