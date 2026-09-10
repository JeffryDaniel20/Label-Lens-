"""Review-workflow HTTP endpoints (P6-T5): decide on a finding, correct an
extracted field."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis import service as analysis_service
from app.analysis.router import AnalysisOut
from app.audit import service as audit
from app.audit.models import AuditAction
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability
from app.review import service
from app.review.models import DecisionAction

router = APIRouter(prefix="/v1", tags=["review"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


class FindingDecisionRequest(BaseModel):
    action: DecisionAction
    reason: str | None = None


class FindingDecisionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    finding_id: uuid.UUID
    analysis_id: uuid.UUID
    action: DecisionAction
    reason: str | None
    actor_label: str | None
    created_at: dt.datetime


class FieldCorrectionRequest(BaseModel):
    field_path: str
    corrected_value: str
    reason: str | None = None


class FieldCorrectionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    analysis_id: uuid.UUID
    child_analysis_id: uuid.UUID
    field_path: str
    original_value: str | None
    corrected_value: str
    created_at: dt.datetime


class FieldCorrectionResponse(BaseModel):
    correction: FieldCorrectionOut
    child_analysis: AnalysisOut


@router.post("/findings/{finding_id}/decision", response_model=FindingDecisionOut, status_code=201)
def decide_finding(
    finding_id: uuid.UUID,
    payload: FindingDecisionRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.FINDING_DECIDE)),
    db: Session = Depends(get_db),
) -> FindingDecisionOut:
    decision = service.record_finding_decision(
        db,
        organization_id=principal.org_id,
        finding_id=finding_id,
        action=payload.action,
        reason=payload.reason,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
    )
    audit.record(
        db,
        action=AuditAction.FINDING_DECIDED,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        resource_type="finding",
        resource_id=finding_id,
        after={"action": payload.action.value},
        ip=_ip(request),
    )
    return FindingDecisionOut.model_validate(decision)


@router.get("/findings/{finding_id}/decisions", response_model=list[FindingDecisionOut])
def list_decisions(
    finding_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[FindingDecisionOut]:
    decisions = service.list_finding_decisions(
        db, organization_id=principal.org_id, finding_id=finding_id
    )
    return [FindingDecisionOut.model_validate(d) for d in decisions]


@router.post(
    "/analyses/{analysis_id}/corrections", response_model=FieldCorrectionResponse, status_code=201
)
def correct_field(
    analysis_id: uuid.UUID,
    payload: FieldCorrectionRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.FINDING_DECIDE)),
    db: Session = Depends(get_db),
) -> FieldCorrectionResponse:
    analysis = analysis_service.get_analysis(
        db, organization_id=principal.org_id, analysis_id=analysis_id
    )
    correction, child = service.create_field_correction(
        db,
        organization_id=principal.org_id,
        analysis=analysis,
        field_path=payload.field_path,
        corrected_value=payload.corrected_value,
        reason=payload.reason,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
    )
    audit.record(
        db,
        action=AuditAction.FIELD_CORRECTED,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        resource_type="analysis",
        resource_id=analysis.id,
        after={"field_path": payload.field_path, "child_analysis_id": str(child.id)},
        ip=_ip(request),
    )
    return FieldCorrectionResponse(
        correction=FieldCorrectionOut.model_validate(correction),
        child_analysis=AnalysisOut.from_analysis(child),
    )
