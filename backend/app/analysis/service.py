"""Analysis creation, idempotency, and history (P5-T1)."""

from __future__ import annotations

import hashlib
import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis, AnalysisEvent, AnalysisState
from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.catalog import service as catalog_service
from app.catalog.models import File, FileStatus, ProductVersion
from app.db.session import tenant_scoped
from app.platform.errors import NotFound, ValidationFailed


def compute_idempotency_key(
    *,
    product_version_id: uuid.UUID,
    file_set_hash: str,
    ruleset_version_id: uuid.UUID | None,
    model_manifest_id: uuid.UUID | None,
) -> str:
    """`sha256(product_version_id + file_set_hash + ruleset_version +
    model_manifest)` (IMPLEMENTATION.md §14) - a `|`-joined, explicit-empty-
    string-for-`None` encoding so the key is deterministic regardless of
    which optional pins are set yet."""
    parts = (
        str(product_version_id),
        file_set_hash,
        str(ruleset_version_id) if ruleset_version_id else "",
        str(model_manifest_id) if model_manifest_id else "",
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def compute_file_set_hash(db: Session, *, organization_id: uuid.UUID, version_id: uuid.UUID) -> str:
    """sha256 over the sorted sha256es of every `ready` file on this
    version - changes whenever the actual file set does, so resubmitting
    after a file is added produces a genuinely new idempotency key."""
    stmt = tenant_scoped(
        select(File).where(
            File.product_version_id == version_id, File.status == FileStatus.READY
        ),
        File,
        organization_id,
    )
    files = db.scalars(stmt).all()
    if not files:
        raise ValidationFailed("This product version has no ready files to analyze.")
    joined = ",".join(sorted(f.sha256 for f in files))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def create_or_get_analysis(
    db: Session,
    *,
    organization_id: uuid.UUID,
    version: ProductVersion,
    file_set_hash: str,
    ruleset_version_id: uuid.UUID | None = None,
    model_manifest_id: uuid.UUID | None = None,
    actor_id: uuid.UUID | None = None,
    actor_type: ActorType = ActorType.USER,
    actor_label: str | None = None,
    correlation_id: str | None = None,
    ip: str | None = None,
) -> tuple[Analysis, bool]:
    """Returns `(analysis, created)`. Re-submitting the same idempotency key
    (same product version + file set + ruleset + model pins) returns the
    existing analysis rather than duplicating spend - regardless of that
    analysis's current state, including a terminal one."""
    if version.organization_id != organization_id:
        raise NotFound("Product version not found.")

    key = compute_idempotency_key(
        product_version_id=version.id,
        file_set_hash=file_set_hash,
        ruleset_version_id=ruleset_version_id,
        model_manifest_id=model_manifest_id,
    )
    existing = db.scalar(
        tenant_scoped(
            select(Analysis).where(Analysis.idempotency_key == key), Analysis, organization_id
        )
    )
    if existing is not None:
        return existing, False

    analysis = Analysis(
        organization_id=organization_id,
        product_version_id=version.id,
        state=AnalysisState.QUEUED,
        ruleset_version_id=ruleset_version_id,
        model_manifest_id=model_manifest_id,
        idempotency_key=key,
        created_by_user_id=actor_id if actor_type is ActorType.USER else None,
    )
    db.add(analysis)
    db.flush()

    # The very first event: from_state=None -> queued. Sequence is always 1
    # here (the analysis row was just created, so no prior event can exist)
    # without needing a query, unlike `state_machine.transition()`'s later calls.
    db.add(
        AnalysisEvent(
            analysis_id=analysis.id,
            organization_id=organization_id,
            sequence=1,
            from_state=None,
            to_state=AnalysisState.QUEUED,
            correlation_id=correlation_id,
        )
    )
    # An analysis existing at all is enough reason to lock the version it
    # references (see `catalog.service.lock_version` - "called by the
    # analysis pipeline the first time a version is analysed"), closing
    # P2-T1's own acceptance criterion now that a real analysis can exist.
    catalog_service.lock_version(
        db, version=version, actor_id=actor_id, actor_type=actor_type, actor_label=actor_label
    )

    audit.record(
        db,
        action=AuditAction.ANALYSIS_SUBMITTED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=organization_id,
        resource_type="analysis",
        resource_id=analysis.id,
        after={"product_version_id": str(version.id), "idempotency_key": key},
        ip=ip,
    )
    return analysis, True


def get_analysis(db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID) -> Analysis:
    stmt = tenant_scoped(
        select(Analysis).where(Analysis.id == analysis_id), Analysis, organization_id
    )
    analysis: Analysis | None = db.scalar(stmt)
    if analysis is None:
        raise NotFound("Analysis not found.")
    return analysis


def get_events(
    db: Session, *, organization_id: uuid.UUID, analysis_id: uuid.UUID
) -> list[AnalysisEvent]:
    stmt = tenant_scoped(
        select(AnalysisEvent).where(AnalysisEvent.analysis_id == analysis_id),
        AnalysisEvent,
        organization_id,
    ).order_by(AnalysisEvent.sequence)
    return list(db.scalars(stmt).all())


def reconstruct_state(events: list[AnalysisEvent]) -> AnalysisState | None:
    """The current state, derived purely from the event log - proof that
    state history is reconstructable independently of `Analysis.state`."""
    if not events:
        return None
    return max(events, key=lambda event: event.sequence).to_state
