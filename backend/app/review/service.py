"""Review-workflow actions (P6-T5): deciding on a finding, and correcting an
extracted field.

Deliberately scoped to what IMPLEMENTATION.md §12 names for *this* task -
Confirm, Override, Fix field, Escalate - not Comment (no acceptance-line
test names it) and not sign-off (P6-T6's own task, "Review queue and
sign-off," not this one's).
"""

from __future__ import annotations

import copy
import uuid
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis, AnalysisEvent, AnalysisState
from app.analysis.stages import STOPPING_STATES, advance_analysis
from app.audit.models import ActorType
from app.extraction import facts as facts_schema
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding
from app.platform.errors import NotFound, ValidationFailed
from app.review.models import (
    OVERRIDE_REASON_MIN_LENGTH,
    DecisionAction,
    FieldCorrection,
    FindingDecision,
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
# failure (which never produced real findings to review to begin with).
_CORRECTABLE_ANALYSIS_STATES: frozenset[AnalysisState] = frozenset(
    {AnalysisState.NEEDS_REVIEW, AnalysisState.REVIEW, AnalysisState.COMPLETED}
)


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
    if analysis.state not in _CORRECTABLE_ANALYSIS_STATES:
        raise ValidationFailed(
            f"Cannot correct a field on an analysis in state {analysis.state.value!r}."
        )

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
