"""Findings HTTP endpoints (P5-T4): list a analysis's findings (filterable
by status/severity, each embedding lightweight evidence refs) and resolve
one finding's full evidence detail (page, bbox, snippet, signed page URL).
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.analysis import service as analysis_service
from app.catalog.models import FilePage
from app.extraction.models import EvidenceSpan, ExtractedField
from app.findings import service as findings_service
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability
from app.platform.config import Settings, get_settings
from app.rules.evaluator import FindingStatus
from app.rules.schema import Severity
from app.storage import service as storage_service
from app.storage.client import ObjectStorageClient

router = APIRouter(prefix="/v1", tags=["findings"])


class EvidenceRefOut(BaseModel):
    extracted_field_id: uuid.UUID
    evidence_span_id: uuid.UUID


class FindingOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    analysis_id: uuid.UUID
    rule_key: str
    rule_version: int
    status: FindingStatus
    severity: Severity
    message: str | None
    details: dict[str, object]
    confidence: float
    evidence_refs: list[EvidenceRefOut]


class EvidenceDetailOut(BaseModel):
    extracted_field_id: uuid.UUID
    field_path: str
    evidence_span_id: uuid.UUID
    file_page_id: uuid.UUID
    page_no: int
    bbox: tuple[float, float, float, float]
    text_snippet: str
    source: str
    page_image_url: str
    page_image_expires_in: int


def _storage_client(request: Request) -> ObjectStorageClient:
    client: ObjectStorageClient = request.app.state.storage_client
    return client


@router.get("/analyses/{analysis_id}/findings", response_model=list[FindingOut])
def list_findings(
    analysis_id: uuid.UUID,
    status: FindingStatus | None = None,
    severity: Severity | None = None,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
) -> list[FindingOut]:
    # 404s (not the tenant's analysis) before ever touching `findings`.
    analysis_service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)

    rows = findings_service.list_findings(
        db,
        organization_id=principal.org_id,
        analysis_id=analysis_id,
        status=status,
        severity=severity,
    )
    out = []
    for row in rows:
        edges = findings_service.get_finding_evidence(
            db, organization_id=principal.org_id, finding_id=row.id
        )
        out.append(
            FindingOut(
                id=row.id,
                analysis_id=row.analysis_id,
                rule_key=row.rule_key,
                rule_version=row.rule_version,
                status=row.status,
                severity=row.severity,
                message=row.message,
                details=row.details,
                confidence=row.confidence,
                evidence_refs=[
                    EvidenceRefOut(
                        extracted_field_id=e.extracted_field_id,
                        evidence_span_id=e.evidence_span_id,
                    )
                    for e in edges
                ],
            )
        )
    return out


@router.get("/findings/{finding_id}/evidence", response_model=list[EvidenceDetailOut])
def get_finding_evidence(
    finding_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ANALYSIS_VIEW)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[EvidenceDetailOut]:
    edges = findings_service.get_finding_evidence(
        db, organization_id=principal.org_id, finding_id=finding_id
    )
    client = _storage_client(request)

    out = []
    for edge in edges:
        field = db.get(ExtractedField, edge.extracted_field_id)
        span = db.get(EvidenceSpan, edge.evidence_span_id)
        assert field is not None and span is not None  # noqa: S101 - FK integrity, not user input
        page = db.get(FilePage, span.file_page_id)
        assert page is not None  # noqa: S101 - FK integrity, not user input
        ticket = storage_service.request_download(
            client,
            organization_id=principal.org_id,
            key=page.render_key,
            ttl_seconds=settings.storage_download_ttl_seconds,
        )
        out.append(
            EvidenceDetailOut(
                extracted_field_id=field.id,
                field_path=field.field_path,
                evidence_span_id=span.id,
                file_page_id=page.id,
                page_no=page.page_no,
                bbox=(span.x1, span.y1, span.x2, span.y2),
                text_snippet=span.text_snippet,
                source=span.source,
                page_image_url=ticket.url,
                page_image_expires_in=ticket.expires_in,
            )
        )
    return out
