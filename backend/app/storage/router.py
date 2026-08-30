"""Storage HTTP endpoints: presigned upload and download URLs.

The API never receives file bytes here - it only authorizes a direct exchange
between the caller and the object store.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.catalog import service as catalog_service
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability
from app.platform.config import Settings, get_settings
from app.platform.errors import ValidationFailed
from app.storage import service as storage_service
from app.storage.client import ObjectStorageClient
from app.storage.keys import ALLOWED_EXTENSIONS, extract_extension

router = APIRouter(prefix="/v1", tags=["storage"])

_ALLOWED_CONTENT_TYPES = {
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
    "heic": "image/heic",
    "tif": "image/tiff",
    "tiff": "image/tiff",
    "pdf": "application/pdf",
}


class UploadRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)


class UploadResponse(BaseModel):
    key: str
    upload_url: str
    expires_in: int


class DownloadResponse(BaseModel):
    url: str
    expires_in: int


def _storage_client(request: Request) -> ObjectStorageClient:
    client: ObjectStorageClient = request.app.state.storage_client
    return client


@router.post(
    "/product-versions/{version_id}/uploads", response_model=UploadResponse, status_code=201
)
def request_upload(
    version_id: uuid.UUID,
    payload: UploadRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.FILE_UPLOAD)),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> UploadResponse:
    version = catalog_service.get_version(db, org_id=principal.org_id, version_id=version_id)
    ext = extract_extension(payload.filename)
    if ext not in ALLOWED_EXTENSIONS or payload.content_type != _ALLOWED_CONTENT_TYPES.get(ext):
        raise ValidationFailed(
            "Unsupported file type.",
            errors=[
                {
                    "field": "filename",
                    "message": f"Allowed types: {', '.join(sorted(ALLOWED_EXTENSIONS))}.",
                }
            ],
        )
    ticket = storage_service.request_upload(
        _storage_client(request),
        organization_id=principal.org_id,
        version=version,
        filename=payload.filename,
        content_type=payload.content_type,
        ttl_seconds=settings.storage_upload_ttl_seconds,
    )
    return UploadResponse(
        key=ticket.key, upload_url=ticket.upload_url, expires_in=ticket.expires_in
    )


@router.get("/files/download-url", response_model=DownloadResponse)
def request_download(
    key: str,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    settings: Settings = Depends(get_settings),
) -> DownloadResponse:
    ticket = storage_service.request_download(
        _storage_client(request),
        organization_id=principal.org_id,
        key=key,
        ttl_seconds=settings.storage_download_ttl_seconds,
    )
    return DownloadResponse(url=ticket.url, expires_in=ticket.expires_in)
