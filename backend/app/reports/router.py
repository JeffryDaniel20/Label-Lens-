"""Report HTTP endpoints (P7-T1/P7-T2): generate a JSON snapshot, render it
to PDF, and read either back - the PDF download itself is always a short-
lived signed URL, never bytes proxied through this API.
"""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.analysis import service as analysis_service
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability
from app.platform.config import Settings, get_settings
from app.reports import service as reports_service
from app.reports.models import Report
from app.storage import service as storage_service
from app.storage.client import ObjectStorageClient

router = APIRouter(prefix="/v1", tags=["reports"])


class ReportOut(BaseModel):
    id: uuid.UUID
    analysis_id: uuid.UUID
    kind: str
    generated_at: dt.datetime
    sha256: str
    pdf_key: str | None
    pdf_download_url: str | None
    pdf_download_expires_in: int | None
    snapshot: dict[str, object]


def _storage_client(request: Request) -> ObjectStorageClient:
    client: ObjectStorageClient = request.app.state.storage_client
    return client


def _report_out(
    report: Report,
    *,
    storage_client: ObjectStorageClient,
    org_id: uuid.UUID,
    settings: Settings,
) -> ReportOut:
    pdf_download_url = None
    pdf_download_expires_in = None
    if report.pdf_key is not None:
        ticket = storage_service.request_download(
            storage_client,
            organization_id=org_id,
            key=report.pdf_key,
            ttl_seconds=settings.storage_download_ttl_seconds,
        )
        pdf_download_url = ticket.url
        pdf_download_expires_in = ticket.expires_in

    return ReportOut(
        id=report.id,
        analysis_id=report.analysis_id,
        kind=report.kind,
        generated_at=report.generated_at,
        sha256=report.sha256,
        pdf_key=report.pdf_key,
        pdf_download_url=pdf_download_url,
        pdf_download_expires_in=pdf_download_expires_in,
        snapshot=report.snapshot,
    )


@router.post("/analyses/{analysis_id}/reports", response_model=ReportOut, status_code=201)
def generate_report(
    analysis_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.REPORT_GENERATE)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReportOut:
    analysis = analysis_service.get_analysis(
        db, organization_id=principal.org_id, analysis_id=analysis_id
    )
    storage_client = _storage_client(request)
    snapshot = reports_service.build_snapshot(db, analysis=analysis, storage_client=storage_client)
    report = reports_service.persist_report(
        db, analysis=analysis, snapshot=snapshot, generated_by=principal.actor_id
    )
    db.commit()
    return _report_out(
        report, storage_client=storage_client, org_id=principal.org_id, settings=settings
    )


@router.get("/analyses/{analysis_id}/reports", response_model=list[ReportOut])
def list_reports(
    analysis_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.REPORT_VIEW)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[ReportOut]:
    analysis_service.get_analysis(db, organization_id=principal.org_id, analysis_id=analysis_id)
    storage_client = _storage_client(request)
    reports = reports_service.list_reports(
        db, organization_id=principal.org_id, analysis_id=analysis_id
    )
    return [
        _report_out(r, storage_client=storage_client, org_id=principal.org_id, settings=settings)
        for r in reports
    ]


@router.post("/reports/{report_id}/pdf", response_model=None, status_code=202)
async def render_report_pdf(
    report_id: uuid.UUID,
    request: Request,
    response: Response,
    principal: Principal = Depends(require(Capability.REPORT_GENERATE)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReportOut | dict[str, bool]:
    """§16 names PDF rendering as its own queue - when a real Arq pool is
    configured, this enqueues `app.reports.worker.render_report_pdf_job` and
    returns 202 immediately (poll `GET /v1/analyses/{id}/reports` for the
    resulting `kind="pdf"` row); with no pool configured (local/test
    default, mirroring `submit_analysis`'s own fallback), it renders inline
    and returns the finished row with 201."""
    json_report = reports_service.get_report(
        db, organization_id=principal.org_id, report_id=report_id
    )
    pool = request.app.state.arq_pool
    if pool is not None:
        await pool.enqueue_job(
            "render_report_pdf_job", str(json_report.id), str(principal.org_id)
        )
        return {"queued": True}

    storage_client = _storage_client(request)
    pdf_report = reports_service.render_and_store_pdf(
        db, json_report=json_report, storage_client=storage_client
    )
    db.commit()
    response.status_code = 201
    return _report_out(
        pdf_report, storage_client=storage_client, org_id=principal.org_id, settings=settings
    )


@router.get("/reports/{report_id}", response_model=ReportOut)
def get_report(
    report_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.REPORT_VIEW)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ReportOut:
    report = reports_service.get_report(
        db, organization_id=principal.org_id, report_id=report_id
    )
    return _report_out(
        report, storage_client=_storage_client(request), org_id=principal.org_id,
        settings=settings,
    )
