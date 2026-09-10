"""Upload-completion HTTP endpoint."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import service as catalog_service
from app.catalog.models import AvStatus, File, FilePage, FileStatus
from app.db.session import tenant_scoped
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability
from app.ingestion import service as ingestion_service
from app.ingestion.av import AvScanner
from app.platform.config import Settings, get_settings
from app.platform.errors import NotFound
from app.storage.client import ObjectStorageClient

router = APIRouter(prefix="/v1", tags=["ingestion"])


class CompleteUploadRequest(BaseModel):
    key: str = Field(min_length=1, max_length=400)
    filename: str = Field(min_length=1, max_length=255)


class FileOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_version_id: uuid.UUID
    original_filename: str
    mime: str
    bytes: int
    page_count: int | None
    status: FileStatus
    av_status: AvStatus
    rejection_reason: str | None
    created_at: dt.datetime


class FilePageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    # `id`/`file_id` were missing until P6-T4 needed them: the label viewer
    # correlates a `Finding`'s evidence (`EvidenceDetailOut.file_page_id`)
    # back to the exact page to open, and `page_no` alone is ambiguous
    # across a multi-file product version (page 1 of file A vs. page 1 of
    # file B) - only the real `file_pages.id` disambiguates.
    id: uuid.UUID
    file_id: uuid.UUID
    page_no: int
    width: int
    height: int
    render_key: str


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _av_scanner(request: Request) -> AvScanner:
    scanner: AvScanner = request.app.state.av_scanner
    return scanner


def _storage_client(request: Request) -> ObjectStorageClient:
    client: ObjectStorageClient = request.app.state.storage_client
    return client


@router.post(
    "/product-versions/{version_id}/files", response_model=FileOut, status_code=201
)
def complete_upload(
    version_id: uuid.UUID,
    payload: CompleteUploadRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.FILE_UPLOAD)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> FileOut:
    version = catalog_service.get_version(db, org_id=principal.org_id, version_id=version_id)
    file_row = ingestion_service.complete_upload(
        db,
        _storage_client(request),
        _av_scanner(request),
        organization_id=principal.org_id,
        version=version,
        key=payload.key,
        original_filename=payload.filename,
        max_upload_bytes=settings.storage_max_upload_bytes,
        max_pdf_pages=settings.ingestion_max_pdf_pages,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        ip=_ip(request),
    )
    return FileOut.model_validate(file_row)


@router.get("/product-versions/{version_id}/files", response_model=list[FileOut])
def list_files(
    version_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> list[FileOut]:
    catalog_service.get_version(db, org_id=principal.org_id, version_id=version_id)
    stmt = tenant_scoped(
        select(File).where(File.product_version_id == version_id), File, principal.org_id
    ).order_by(File.created_at.desc())
    return [FileOut.model_validate(f) for f in db.scalars(stmt).all()]


@router.get("/files/{file_id}/pages", response_model=list[FilePageOut])
def list_file_pages(
    file_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> list[FilePageOut]:
    file_row = db.scalar(
        tenant_scoped(select(File).where(File.id == file_id), File, principal.org_id)
    )
    if file_row is None:
        raise NotFound("File not found.")
    stmt = tenant_scoped(
        select(FilePage).where(FilePage.file_id == file_id), FilePage, principal.org_id
    ).order_by(FilePage.page_no)
    return [FilePageOut.model_validate(p) for p in db.scalars(stmt).all()]
