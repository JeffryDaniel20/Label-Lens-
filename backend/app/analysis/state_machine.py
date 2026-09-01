"""The analysis state machine (P5-T1): legal-transition enforcement.

IMPLEMENTATION.md §14's diagram, as an explicit transition table rather than
scattered conditionals:

    queued -> validating -> preprocessing -> ocr -> extracting -> normalizing
           -> classifying -> rule_eval -> scoring -> (needs_review | completed)
    needs_review -> review -> completed
    any non-terminal -> failed | cancelled
    completed | failed | cancelled -> (nothing; terminal)

`transition()` is the only function in this codebase that changes
`Analysis.state` - it is the sole place an illegal transition can be
rejected, and every call also appends the `AnalysisEvent` that makes state
history reconstructable independently of the `Analysis` row itself.
"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.analysis.models import TERMINAL_STATES, Analysis, AnalysisEvent, AnalysisState
from app.db.base import utcnow
from app.platform.errors import StateInvalid

_S = AnalysisState

ALLOWED_TRANSITIONS: dict[AnalysisState, frozenset[AnalysisState]] = {
    _S.QUEUED: frozenset({_S.VALIDATING, _S.FAILED, _S.CANCELLED}),
    _S.VALIDATING: frozenset({_S.PREPROCESSING, _S.FAILED, _S.CANCELLED}),
    _S.PREPROCESSING: frozenset({_S.OCR, _S.FAILED, _S.CANCELLED}),
    _S.OCR: frozenset({_S.EXTRACTING, _S.FAILED, _S.CANCELLED}),
    _S.EXTRACTING: frozenset({_S.NORMALIZING, _S.FAILED, _S.CANCELLED}),
    _S.NORMALIZING: frozenset({_S.CLASSIFYING, _S.FAILED, _S.CANCELLED}),
    _S.CLASSIFYING: frozenset({_S.RULE_EVAL, _S.FAILED, _S.CANCELLED}),
    _S.RULE_EVAL: frozenset({_S.SCORING, _S.FAILED, _S.CANCELLED}),
    _S.SCORING: frozenset({_S.NEEDS_REVIEW, _S.COMPLETED, _S.FAILED, _S.CANCELLED}),
    _S.NEEDS_REVIEW: frozenset({_S.REVIEW, _S.FAILED, _S.CANCELLED}),
    _S.REVIEW: frozenset({_S.COMPLETED, _S.FAILED, _S.CANCELLED}),
    _S.COMPLETED: frozenset(),
    _S.FAILED: frozenset(),
    _S.CANCELLED: frozenset(),
}


def next_sequence(db: Session, analysis_id: uuid.UUID) -> int:
    count = db.scalar(
        select(func.count())
        .select_from(AnalysisEvent)
        .where(AnalysisEvent.analysis_id == analysis_id)
    )
    return (count or 0) + 1


def transition(
    db: Session,
    analysis: Analysis,
    to_state: AnalysisState,
    *,
    correlation_id: str | None = None,
    worker_id: str | None = None,
    reason: str | None = None,
    failure_stage: str | None = None,
    retryable: bool | None = None,
) -> Analysis:
    """Move `analysis` to `to_state`, or raise `StateInvalid` (409) if that
    transition isn't legal from its current state."""
    from_state = analysis.state
    if to_state not in ALLOWED_TRANSITIONS.get(from_state, frozenset()):
        raise StateInvalid(
            f"Cannot transition analysis {analysis.id} from "
            f"{from_state.value!r} to {to_state.value!r}."
        )

    analysis.state = to_state
    if to_state in TERMINAL_STATES:
        analysis.finished_at = utcnow()
    if to_state is AnalysisState.FAILED:
        analysis.failure_stage = failure_stage or from_state.value
        analysis.retryable = retryable if retryable is not None else False

    db.add(
        AnalysisEvent(
            analysis_id=analysis.id,
            organization_id=analysis.organization_id,
            sequence=next_sequence(db, analysis.id),
            from_state=from_state,
            to_state=to_state,
            correlation_id=correlation_id,
            worker_id=worker_id,
            reason=reason,
        )
    )
    db.flush()
    return analysis
