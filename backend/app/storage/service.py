"""Storage domain service: ties presigned URLs to tenant ownership.

Ownership is checked from the object key itself (see `keys.key_belongs_to_org`)
so that issuing a download or upload URL never requires trusting anything the
caller sent beyond the key format - a cross-tenant key is rejected before any
call reaches the object store.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.catalog.models import ProductVersion
from app.platform.errors import NotFound
from app.storage.client import ObjectStorageClient
from app.storage.keys import build_object_key, key_belongs_to_org


@dataclass(slots=True, frozen=True)
class UploadTicket:
    key: str
    upload_url: str
    expires_in: int


@dataclass(slots=True, frozen=True)
class DownloadTicket:
    url: str
    expires_in: int


def request_upload(
    client: ObjectStorageClient,
    *,
    organization_id: uuid.UUID,
    version: ProductVersion,
    filename: str,
    content_type: str,
    ttl_seconds: int,
) -> UploadTicket:
    if version.organization_id != organization_id:
        # Mirrors the 404-not-403 convention used everywhere else: a caller
        # outside this tenant must not learn that the version exists.
        raise NotFound("Product version not found.")
    key = build_object_key(
        organization_id=organization_id, product_version_id=version.id, filename=filename
    )
    url = client.generate_presigned_put(key, content_type=content_type, expires_in=ttl_seconds)
    return UploadTicket(key=key, upload_url=url, expires_in=ttl_seconds)


def request_download(
    client: ObjectStorageClient,
    *,
    organization_id: uuid.UUID,
    key: str,
    ttl_seconds: int,
) -> DownloadTicket:
    if not key_belongs_to_org(key, organization_id):
        raise NotFound("File not found.")
    url = client.generate_presigned_get(key, expires_in=ttl_seconds)
    return DownloadTicket(url=url, expires_in=ttl_seconds)
