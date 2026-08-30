"""Upload-completion orchestration.

`complete_upload()` is the one place untrusted bytes are actually inspected:
size, magic bytes vs. claimed type, PDF structure, antivirus, then dedup
against everything else already stored for the organization. Nothing here
trusts the client-supplied filename or Content-Type beyond using them as a
hint to cross-check against what the bytes actually are.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.catalog.models import AvStatus, File, FilePage, FileStatus, ProductVersion
from app.ingestion.av import AvScanner, AvVerdict
from app.ingestion.magic_bytes import content_type_matches_extension, sniff_mime
from app.ingestion.pdf_checks import validate_pdf
from app.ingestion.rasterize import RasterizationFailed, rasterize
from app.platform.errors import NotFound, ValidationFailed
from app.storage.client import ObjectStorageClient
from app.storage.keys import (
    build_render_key,
    extract_extension,
    key_belongs_to_org,
    key_product_version_id,
)


@dataclass(slots=True, frozen=True)
class _Rejection(Exception):
    reason: str


def complete_upload(
    db: Session,
    storage: ObjectStorageClient,
    av_scanner: AvScanner,
    *,
    organization_id: uuid.UUID,
    version: ProductVersion,
    key: str,
    original_filename: str,
    max_upload_bytes: int,
    max_pdf_pages: int,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    ip: str | None = None,
) -> File:
    if version.organization_id != organization_id:
        raise NotFound("Product version not found.")
    if not key_belongs_to_org(key, organization_id) or key_product_version_id(key) != version.id:
        raise NotFound("Upload not found.")

    meta = storage.head_object(key)
    if meta is None:
        raise ValidationFailed("No file was found at that upload key. Upload the file first.")
    if meta.size_bytes <= 0:
        storage.delete_object(key)
        raise ValidationFailed("The uploaded file is empty.")
    if meta.size_bytes > max_upload_bytes:
        storage.delete_object(key)
        raise ValidationFailed(
            f"The uploaded file is {meta.size_bytes} bytes; "
            f"the maximum is {max_upload_bytes} bytes."
        )

    data = storage.download_object(key)

    sha256 = hashlib.sha256(data).hexdigest()

    try:
        mime, page_count = _validate_content(
            data, original_filename=original_filename, max_pdf_pages=max_pdf_pages
        )
        av_status = _scan(av_scanner, data)
        pages = _get_or_render_pages(
            db,
            storage,
            organization_id=organization_id,
            product_version_id=version.id,
            sha256=sha256,
            data=data,
            mime=mime,
        )
    except _Rejection as exc:
        storage.delete_object(key)
        audit.record_out_of_band(
            action=AuditAction.FILE_REJECTED,
            actor_type=actor_type,
            actor_id=actor_id,
            actor_label=actor_label,
            organization_id=organization_id,
            resource_type="file",
            resource_id=key,
            after={"reason": exc.reason, "filename": original_filename},
            ip=ip,
        )
        raise ValidationFailed(exc.reason) from None

    final_key = _dedup(storage, db, organization_id=organization_id, sha256=sha256, new_key=key)

    file_row = File(
        organization_id=organization_id,
        product_version_id=version.id,
        storage_key=final_key,
        original_filename=original_filename[:255],
        sha256=sha256,
        mime=mime,
        bytes=meta.size_bytes,
        page_count=page_count,
        status=FileStatus.READY,
        av_status=av_status,
        created_by_user_id=actor_id if actor_type is ActorType.USER else None,
    )
    db.add(file_row)
    db.flush()
    for page in pages:
        db.add(
            FilePage(
                organization_id=organization_id,
                file_id=file_row.id,
                page_no=page.page_no,
                width=page.width,
                height=page.height,
                render_key=page.render_key,
            )
        )
    db.flush()
    audit.record(
        db,
        action=AuditAction.FILE_UPLOADED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=organization_id,
        resource_type="file",
        resource_id=file_row.id,
        after={
            "sha256": sha256,
            "mime": mime,
            "bytes": meta.size_bytes,
            "deduped": final_key != key,
            "page_count": len(pages),
        },
        ip=ip,
    )
    return file_row


def _validate_content(
    data: bytes, *, original_filename: str, max_pdf_pages: int
) -> tuple[str, int | None]:
    mime = sniff_mime(data)
    if mime is None:
        raise _Rejection("The file's content does not match any supported file type.")

    extension = extract_extension(original_filename)
    if not content_type_matches_extension(mime, extension):
        raise _Rejection(
            "The file's content does not match its filename extension. "
            "This is rejected as a possible spoofed upload."
        )

    if mime == "application/pdf":
        result = validate_pdf(data, max_pages=max_pdf_pages)
        if not result.ok:
            raise _Rejection(result.reason)
        return mime, result.page_count

    return mime, None


def _scan(av_scanner: AvScanner, data: bytes) -> AvStatus:
    verdict, signature = av_scanner.scan(data)
    if verdict is AvVerdict.INFECTED:
        raise _Rejection(f"Antivirus scan detected a threat: {signature or 'unknown signature'}.")
    return AvStatus(verdict.value)


@dataclass(slots=True, frozen=True)
class _PageDescriptor:
    page_no: int
    width: int
    height: int
    render_key: str


def _get_or_render_pages(
    db: Session,
    storage: ObjectStorageClient,
    *,
    organization_id: uuid.UUID,
    product_version_id: uuid.UUID,
    sha256: str,
    data: bytes,
    mime: str,
) -> list[_PageDescriptor]:
    """Rasterize `data`, or reuse renders already produced for this content.

    Renders are content-addressed by sha256 (see `build_render_key`), so if
    any other file in the organization with identical content has already
    been rasterized, its page rows are reused verbatim instead of paying to
    decode and re-store the same pixels twice.
    """
    existing = db.scalars(
        select(FilePage)
        .join(File, File.id == FilePage.file_id)
        .where(File.organization_id == organization_id, File.sha256 == sha256)
        .order_by(FilePage.page_no)
    ).all()
    if existing:
        return [
            _PageDescriptor(
                page_no=p.page_no, width=p.width, height=p.height, render_key=p.render_key
            )
            for p in existing
        ]

    try:
        rendered = rasterize(data, mime=mime)
    except RasterizationFailed as exc:
        raise _Rejection(exc.reason) from None

    descriptors: list[_PageDescriptor] = []
    for page in rendered:
        render_key = build_render_key(
            organization_id=organization_id,
            product_version_id=product_version_id,
            sha256=sha256,
            page_no=page.page_no,
        )
        storage.put_object(render_key, page.png_bytes, content_type="image/png")
        descriptors.append(
            _PageDescriptor(
                page_no=page.page_no, width=page.width, height=page.height, render_key=render_key
            )
        )
    return descriptors


def _dedup(
    storage: ObjectStorageClient,
    db: Session,
    *,
    organization_id: uuid.UUID,
    sha256: str,
    new_key: str,
) -> str:
    existing = db.scalar(
        select(File).where(File.organization_id == organization_id, File.sha256 == sha256)
    )
    if existing is None:
        return new_key
    storage.delete_object(new_key)
    return existing.storage_key
