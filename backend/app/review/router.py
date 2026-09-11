"""Review-workflow HTTP endpoints: decide on a finding, correct an
extracted field (P6-T5); the review queue, reviewer assignment, and
sign-off (P6-T6)."""

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


class AssignReviewerRequest(BaseModel):
    reviewer_id: uuid.UUID | None = None


class ReviewQueueEntryOut(BaseModel):
    analysis: AnalysisOut
    sla_since: dt.datetime


class SignoffRequest(BaseModel):
    pass


class SignoffOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    analysis_id: uuid.UUID
    ruleset_version_id: uuid.UUID | None
    finding_set_hash: str
    signed_off_at: dt.datetime
    actor_label: str | None


@router.get(
    # `/review/queue`, not `/analyses/queue`: `app.analysis.router`'s own
    # `GET /analyses/{analysis_id}` is registered before this router in
    # `app.main` and would otherwise match `queue` as a (UUID-invalid)
    # `analysis_id` first, 422ing before this real route was ever reached -
    # a distinct prefix sidesteps the whole path-collision class of bug
    # rather than depending on router registration order.
    "/review/queue",
    response_model=list[ReviewQueueEntryOut],
)
def get_review_queue(
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[ReviewQueueEntryOut]:
    entries = service.list_review_queue(db, organization_id=principal.org_id)
    return [
        ReviewQueueEntryOut(analysis=AnalysisOut.from_analysis(analysis), sla_since=sla_since)
        for analysis, sla_since in entries
    ]


@router.patch("/analyses/{analysis_id}/assignment", response_model=AnalysisOut)
def assign_reviewer(
    analysis_id: uuid.UUID,
    payload: AssignReviewerRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.FINDING_DECIDE)),
    db: Session = Depends(get_db),
) -> AnalysisOut:
    analysis = analysis_service.get_analysis(
        db, organization_id=principal.org_id, analysis_id=analysis_id
    )
    service.assign_reviewer(
        db, organization_id=principal.org_id, analysis=analysis, reviewer_id=payload.reviewer_id
    )
    audit.record(
        db,
        action=AuditAction.ANALYSIS_ASSIGNED,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        resource_type="analysis",
        resource_id=analysis.id,
        after={"reviewer_id": str(payload.reviewer_id) if payload.reviewer_id else None},
        ip=_ip(request),
    )
    return AnalysisOut.from_analysis(analysis)


@router.post("/analyses/{analysis_id}/signoff", response_model=SignoffOut, status_code=201)
def sign_off_analysis(
    analysis_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ANALYSIS_SIGNOFF)),
    db: Session = Depends(get_db),
) -> SignoffOut:
    analysis = analysis_service.get_analysis(
        db, organization_id=principal.org_id, analysis_id=analysis_id
    )
    signoff = service.sign_off_analysis(
        db,
        organization_id=principal.org_id,
        analysis=analysis,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
    )
    audit.record(
        db,
        action=AuditAction.ANALYSIS_SIGNED_OFF,
        actor_type=principal.actor_type,
        actor_id=principal.actor_id,
        actor_label=principal.actor_label,
        organization_id=principal.org_id,
        resource_type="analysis",
        resource_id=analysis.id,
        after={"finding_set_hash": signoff.finding_set_hash},
        ip=_ip(request),
    )
    return SignoffOut.model_validate(signoff)


@router.get("/analyses/{analysis_id}/signoff", response_model=SignoffOut | None)
def get_signoff(
    analysis_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> SignoffOut | None:
    analysis_service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
    signoff = service.get_signoff(db, organization_id=principal.org_id, analysis_id=analysis_id)
    return SignoffOut.model_validate(signoff) if signoff else None
