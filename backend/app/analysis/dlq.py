"""Dead-letter queue: recording, listing, and replaying stage jobs that
won't be automatically retried further (P5-T3)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import (
    Analysis,
    AnalysisEvent,
    AnalysisState,
    DeadLetterJob,
    DeadLetterReason,
)
from app.analysis.service import compute_idempotency_key
from app.catalog import service as catalog_service
from app.db.base import utcnow
from app.db.session import tenant_scoped
from app.platform.errors import NotFound, ValidationFailed


def record_dead_letter(
    db: Session,
    *,
    analysis: Analysis,
    stage: str,
    reason: DeadLetterReason,
    error_message: str,
    attempt_count: int,
) -> DeadLetterJob:
    dlq = DeadLetterJob(
        analysis_id=analysis.id,
        organization_id=analysis.organization_id,
        stage=stage,
        reason=reason,
        error_message=error_message[:2000],
        attempt_count=attempt_count,
    )
    db.add(dlq)
    db.flush()
    return dlq


def list_dead_letters(
    db: Session, *, organization_id: uuid.UUID, unreplayed_only: bool = False
) -> list[DeadLetterJob]:
    stmt = tenant_scoped(select(DeadLetterJob), DeadLetterJob, organization_id)
    if unreplayed_only:
        stmt = stmt.where(DeadLetterJob.replayed_at.is_(None))
    stmt = stmt.order_by(DeadLetterJob.created_at.desc())
    return list(db.scalars(stmt).all())


def get_dead_letter(
    db: Session, *, organization_id: uuid.UUID, dead_letter_id: uuid.UUID
) -> DeadLetterJob:
    stmt = tenant_scoped(
        select(DeadLetterJob).where(DeadLetterJob.id == dead_letter_id),
        DeadLetterJob,
        organization_id,
    )
    dlq: DeadLetterJob | None = db.scalar(stmt)
    if dlq is None:
        raise NotFound("Dead-letter record not found.")
    return dlq


def replay_dead_letter(
    db: Session, *, organization_id: uuid.UUID, dead_letter_id: uuid.UUID
) -> Analysis:
    """Creates a **new** analysis for the same product version, rather than
    resurrecting the dead-lettered one - that analysis is `failed` (terminal),
    and PostgreSQL itself rejects any further mutation of a terminal analysis
    row (migration 0006), which is a deliberate guarantee this replay must
    not try to work around. A fresh attempt is the only correct way to retry.

    Only a `retryable` dead letter (a transient/timeout/stalled failure) can
    be replayed - a `permanent_error` dead letter means the input itself was
    the problem, and blindly re-running it would just fail the same way."""
    dlq = get_dead_letter(db, organization_id=organization_id, dead_letter_id=dead_letter_id)
    if dlq.replayed_at is not None:
        raise ValidationFailed("This dead-letter record has already been replayed.")

    dead_analysis = db.get(Analysis, dlq.analysis_id)
    if dead_analysis is None or dead_analysis.organization_id != organization_id:
        raise NotFound("The original analysis for this dead-letter record no longer exists.")
    if not dead_analysis.retryable:
        raise ValidationFailed(
            "This dead-letter record is not retryable (a permanent failure) - "
            "fix the underlying input rather than replaying it unchanged."
        )

    # A deliberate, operator-triggered fresh attempt is a different action
    # from the original (accidental-resubmission-preventing) idempotency
    # check, so it gets its own key rather than colliding with - and being
    # silently absorbed into - the original, now-terminal analysis.
    replay_key = compute_idempotency_key(
        product_version_id=dead_analysis.product_version_id,
        file_set_hash=f"replay:{dlq.id}:{uuid.uuid4()}",
        ruleset_version_id=dead_analysis.ruleset_version_id,
        model_manifest_id=dead_analysis.model_manifest_id,
    )
    new_analysis = Analysis(
        organization_id=organization_id,
        product_version_id=dead_analysis.product_version_id,
        state=AnalysisState.QUEUED,
        ruleset_version_id=dead_analysis.ruleset_version_id,
        model_manifest_id=dead_analysis.model_manifest_id,
        idempotency_key=replay_key,
        created_by_user_id=dead_analysis.created_by_user_id,
    )
    db.add(new_analysis)
    db.flush()

    db.add(
        AnalysisEvent(
            analysis_id=new_analysis.id,
            organization_id=organization_id,
            sequence=1,
            from_state=None,
            to_state=AnalysisState.QUEUED,
            reason=f"Replay of dead-letter record {dlq.id} (original analysis {dead_analysis.id}).",
        )
    )

    version = catalog_service.get_version(
        db, org_id=organization_id, version_id=dead_analysis.product_version_id
    )
    catalog_service.lock_version(db, version=version)

    dlq.replayed_at = utcnow()
    dlq.replayed_as_analysis_id = new_analysis.id
    db.flush()
    return new_analysis
