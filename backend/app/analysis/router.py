"""Analysis HTTP endpoints: submit, view, view history, cancel (P5-T1);
dead-letter listing and replay (P5-T3).

Submission does not yet enqueue the first worker job (see
`app.analysis.worker.enqueue_first_stage`'s own docstring for why) - this
router proves the entity, its idempotency, and its state machine end-to-end
over real HTTP independent of that wiring.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis import dlq, service, sse
from app.analysis.models import Analysis, AnalysisState, ConfidenceTier, DeadLetterReason
from app.analysis.stages import progress_percentage
from app.analysis.state_machine import transition
from app.audit import service as audit
from app.audit.models import AuditAction
from app.catalog import service as catalog_service
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability

router = APIRouter(prefix="/v1", tags=["analysis"])


class AnalysisOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_version_id: uuid.UUID
    state: AnalysisState
    confidence_tier: ConfidenceTier | None
    failure_stage: str | None
    retryable: bool | None
    started_at: dt.datetime
    finished_at: dt.datetime | None
    progress_percentage: int
    total_tokens_in: int
    total_tokens_out: int
    total_cost_cents: int

    @classmethod
    def from_analysis(cls, analysis: Analysis) -> AnalysisOut:
        return cls(
            id=analysis.id,
            product_version_id=analysis.product_version_id,
            state=analysis.state,
            confidence_tier=analysis.confidence_tier,
            failure_stage=analysis.failure_stage,
            retryable=analysis.retryable,
            started_at=analysis.started_at,
            finished_at=analysis.finished_at,
            progress_percentage=progress_percentage(analysis),
            total_tokens_in=analysis.total_tokens_in,
            total_tokens_out=analysis.total_tokens_out,
            total_cost_cents=analysis.total_cost_cents,
        )


class AnalysisEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sequence: int
    from_state: AnalysisState | None
    to_state: AnalysisState
    occurred_at: dt.datetime
    reason: str | None


class DeadLetterOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    analysis_id: uuid.UUID
    stage: str
    reason: DeadLetterReason
    error_message: str
    attempt_count: int
    created_at: dt.datetime
    replayed_at: dt.datetime | None
    replayed_as_analysis_id: uuid.UUID | None


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _correlation_id(request: Request) -> str | None:
    value = request.headers.get("X-Correlation-Id")
    return value if value else None


@router.post(
    "/product-versions/{version_id}/analyses", response_model=AnalysisOut
)
def submit_analysis(
    version_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require(Capability.ANALYSIS_RUN)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    version = catalog_service.get_version(db, org_id=principal.org_id, version_id=version_id)
    file_set_hash = service.compute_file_set_hash(
        db, organization_id=principal.org_id, version_id=version.id
    )
    analysis, created = service.create_or_get_analysis(
        db,
        organization_id=principal.org_id,
        version=version,
        file_set_hash=file_set_hash,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        correlation_id=_correlation_id(request),
        ip=_ip(request),
    )
    response.status_code = 201 if created else 200
    return AnalysisOut.from_analysis(analysis)


@router.get("/analyses/dead-letters", response_model=list[DeadLetterOut])
def list_dead_letters(
    unreplayed_only: bool = False,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[DeadLetterOut]:
    # Declared before `/analyses/{analysis_id}` on purpose: FastAPI matches
    # routes in declaration order, and `{analysis_id}` is typed `uuid.UUID` -
    # if this route were declared after, "dead-letters" would still fail to
    # parse as a UUID against that route (422) rather than falling through
    # to this one, since routing happens on the path shape, not on retrying
    # a failed parse against the next candidate.
    records = dlq.list_dead_letters(
        db, organization_id=principal.org_id, unreplayed_only=unreplayed_only
    )
    return [DeadLetterOut.model_validate(r) for r in records]


@router.post("/analyses/dead-letters/{dead_letter_id}/replay", response_model=AnalysisOut)
def replay_dead_letter(
    dead_letter_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_RUN)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    new_analysis = dlq.replay_dead_letter(
        db, organization_id=principal.org_id, dead_letter_id=dead_letter_id
    )
    return AnalysisOut.from_analysis(new_analysis)


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
def get_analysis(
    analysis_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    analysis = service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
    return AnalysisOut.from_analysis(analysis)


@router.get("/analyses/{analysis_id}/events", response_model=None)
async def list_analysis_events(
    analysis_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[AnalysisEventOut] | StreamingResponse:
    """JSON history by default; a Server-Sent Events live stream (P5-T5)
    when the client asks for `text/event-stream` - the same path, per
    IMPLEMENTATION.md §7's API table, rather than a second endpoint.
    `response_model=None` (unlike every other route here): the return type
    is a union including `StreamingResponse`, which isn't a valid Pydantic
    field - FastAPI still returns a `StreamingResponse` byte-for-byte as
    given and JSON-encodes the list in the other branch via its default
    `jsonable_encoder`, so nothing about the actual response shape changes."""
    service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)

    if "text/event-stream" in request.headers.get("accept", ""):
        since_sequence = 0
        last_event_id = request.headers.get("last-event-id")
        if last_event_id is not None:
            try:
                since_sequence = int(last_event_id)
            except ValueError:
                since_sequence = 0
        return StreamingResponse(
            sse.stream_analysis_events(
                db,
                organization_id=principal.org_id,
                analysis_id=analysis_id,
                since_sequence=since_sequence,
            ),
            media_type="text/event-stream",
        )

    events = service.get_events(db, organization_id=principal.org_id, analysis_id=analysis_id)
    return [AnalysisEventOut.model_validate(e) for e in events]


@router.post("/analyses/{analysis_id}/cancel", response_model=AnalysisOut)
def cancel_analysis(
    analysis_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ANALYSIS_RUN)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    analysis = service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
    transition(
        db,
        analysis,
        AnalysisState.CANCELLED,
        correlation_id=_correlation_id(request),
        reason="Cancelled by user request.",
    )
    audit.record(
        db,
        action=AuditAction.ANALYSIS_CANCELLED,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        resource_type="analysis",
        resource_id=analysis.id,
        ip=_ip(request),
    )
    return AnalysisOut.from_analysis(analysis)
