"""Analysis HTTP endpoints (P5-T1): submit, view, view history, cancel.

No worker exists yet to actually advance a submitted analysis past
`queued` (that's P5-T2) - this router only proves the entity, its
idempotency, and its state machine end-to-end over real HTTP.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis import service
from app.analysis.models import AnalysisState, ConfidenceTier
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


class AnalysisEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    sequence: int
    from_state: AnalysisState | None
    to_state: AnalysisState
    occurred_at: dt.datetime
    reason: str | None


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
    return AnalysisOut.model_validate(analysis)


@router.get("/analyses/{analysis_id}", response_model=AnalysisOut)
def get_analysis(
    analysis_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    analysis = service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
    return AnalysisOut.model_validate(analysis)


@router.get("/analyses/{analysis_id}/events", response_model=list[AnalysisEventOut])
def list_analysis_events(
    analysis_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[AnalysisEventOut]:
    service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
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
    return AnalysisOut.model_validate(analysis)
