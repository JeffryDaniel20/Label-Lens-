"""Retention purge, two-phase org/product deletion, and the DSR export.

IMPLEMENTATION.md 16's three commitments, made real:

- **Retention** ("a nightly job purges expired objects and records the purge
  in the audit trail"): `purge_expired_files` deletes only the object-storage
  bytes behind a `File`/`FilePage` older than the org's `retention_days` -
  never the row itself, and never anything downstream. Findings, evidence
  spans (`text_snippet` is plain text, not an image) and reports were all
  already fully assembled from the pipeline before any purge runs, so they
  stay intact by construction, not by a special case here.
- **Deletion** ("two-phase soft delete (30-day window) then hard purge"):
  `request_organization_deletion`/`request_product_deletion` set the
  `deleted_at` column both models already carry; `purge_deleted_organizations`/
  `purge_deleted_products` hard-delete anything still soft-deleted past the
  grace window, deleting every real object-storage key first (the DB cascade
  that follows only removes rows, never bytes in the object store) and
  writing one audit entry per resource *before* the row disappears, since
  `audit_logs.organization_id` is `ON DELETE SET NULL` precisely so the fact
  of the deletion outlives the row it describes.
- **DSR**: `export_user_data` is the "access/export" half for the one kind of
  personal data this application's own request model actually scopes per
  request - a member's account within their current organization. It does not
  attempt a cross-org export (nothing else in this codebase reads a user's
  data across tenants either; `Principal` is always bound to exactly one
  org), and that scope is stated here rather than silently assumed.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction, AuditLog
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.db.base import utcnow
from app.identity.models import Membership, Organization, User
from app.platform.errors import StateInvalid
from app.reports.models import Report
from app.storage.client import ObjectStorageClient

DELETION_GRACE_DAYS = 30


def _as_utc(value: dt.datetime) -> dt.datetime:
    """SQLite (used by the test suite and small deployments) does not
    preserve `tzinfo` on a `DateTime(timezone=True)` column - a value
    written aware comes back naive after a round trip through the DB. Every
    comparison against `utcnow()` in this module normalizes through here
    first so it never depends on whether the value already made that trip."""
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


# --------------------------------------------------------------------------
# Retention purge (expired source files)
# --------------------------------------------------------------------------


def purge_expired_files(
    db: Session, *, storage_client: ObjectStorageClient, now: dt.datetime | None = None
) -> list[uuid.UUID]:
    """Delete object-storage bytes for every `File` past its org's
    `retention_days`, across every non-deleted organization. Returns the
    purged file ids. Safe to call repeatedly - already-purged files
    (`purged_at` set) are skipped."""
    now = now or utcnow()
    purged: list[uuid.UUID] = []

    orgs = db.scalars(select(Organization).where(Organization.deleted_at.is_(None))).all()
    for org in orgs:
        cutoff = now - dt.timedelta(days=org.retention_days)
        files = db.scalars(
            select(File).where(
                File.organization_id == org.id,
                File.purged_at.is_(None),
                File.status == FileStatus.READY,
                File.created_at < cutoff,
            )
        ).all()
        for file in files:
            _purge_file(db, storage_client, file=file, org=org, reason="retention")
            purged.append(file.id)
    return purged


def _purge_file(
    db: Session,
    storage_client: ObjectStorageClient,
    *,
    file: File,
    org: Organization,
    reason: str,
) -> None:
    if not _storage_key_still_referenced(db, file=file, key=file.storage_key):
        storage_client.delete_object(file.storage_key)

    pages = db.scalars(select(FilePage).where(FilePage.file_id == file.id)).all()
    for page in pages:
        if page.purged_at is None and not _render_key_still_referenced(
            db, page=page, key=page.render_key
        ):
            storage_client.delete_object(page.render_key)
        page.purged_at = utcnow()

    file.purged_at = utcnow()
    audit.record(
        db,
        action=AuditAction.FILE_PURGED,
        actor_type=ActorType.SYSTEM,
        organization_id=org.id,
        resource_type="file",
        resource_id=file.id,
        after={"reason": reason, "storage_key": file.storage_key},
    )
    db.flush()


def _storage_key_still_referenced(db: Session, *, file: File, key: str) -> bool:
    """Two `File` rows dedup to the same `storage_key` (see `File`'s own
    docstring); don't delete an object another, still-live file still
    points at."""
    other = db.scalar(
        select(File.id).where(
            File.organization_id == file.organization_id,
            File.storage_key == key,
            File.id != file.id,
            File.purged_at.is_(None),
        )
    )
    return other is not None


def _render_key_still_referenced(db: Session, *, page: FilePage, key: str) -> bool:
    other = db.scalar(
        select(FilePage.id).where(
            FilePage.organization_id == page.organization_id,
            FilePage.render_key == key,
            FilePage.id != page.id,
            FilePage.purged_at.is_(None),
        )
    )
    return other is not None


# --------------------------------------------------------------------------
# Two-phase deletion: organizations
# --------------------------------------------------------------------------


def request_organization_deletion(
    db: Session,
    *,
    organization: Organization,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    ip: str | None = None,
) -> Organization:
    if organization.deleted_at is not None:
        raise StateInvalid("This organization is already scheduled for deletion.")
    organization.deleted_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=AuditAction.ORG_DELETE_REQUESTED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=organization.id,
        resource_type="organization",
        resource_id=organization.id,
        after={
            "deleted_at": organization.deleted_at.isoformat(),
            "purge_after": (
                organization.deleted_at + dt.timedelta(days=DELETION_GRACE_DAYS)
            ).isoformat(),
        },
        ip=ip,
    )
    return organization


def restore_organization(
    db: Session,
    *,
    organization: Organization,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    ip: str | None = None,
) -> Organization:
    if organization.deleted_at is None:
        raise StateInvalid("This organization is not scheduled for deletion.")
    if utcnow() - _as_utc(organization.deleted_at) > dt.timedelta(days=DELETION_GRACE_DAYS):
        raise StateInvalid(
            "The 30-day restore window has passed; this organization can no longer be restored."
        )
    organization.deleted_at = None
    db.flush()
    audit.record(
        db,
        action=AuditAction.ORG_RESTORED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=organization.id,
        resource_type="organization",
        resource_id=organization.id,
        ip=ip,
    )
    return organization


@dataclass(slots=True, frozen=True)
class PurgeReport:
    organizations_purged: list[uuid.UUID] = field(default_factory=list)
    products_purged: list[uuid.UUID] = field(default_factory=list)


def _org_storage_keys(db: Session, *, org_id: uuid.UUID) -> list[str]:
    keys: list[str] = list(
        db.scalars(select(File.storage_key).where(File.organization_id == org_id))
    )
    keys += list(db.scalars(select(FilePage.render_key).where(FilePage.organization_id == org_id)))
    keys += [
        k
        for k in db.scalars(
            select(Report.pdf_key).where(
                Report.organization_id == org_id, Report.pdf_key.is_not(None)
            )
        )
        if k is not None
    ]
    return sorted(set(keys))


def purge_deleted_organizations(
    db: Session,
    *,
    storage_client: ObjectStorageClient,
    now: dt.datetime | None = None,
    grace_days: int = DELETION_GRACE_DAYS,
) -> list[uuid.UUID]:
    """Hard-delete every organization soft-deleted past the grace window:
    every real object-storage key it owns is deleted first, then one audit
    entry records the purge (its `organization_id` is nulled by the
    `ON DELETE SET NULL` FK the instant the row below is gone - the row
    persists, only its link to the vanished org does not), and only then is
    the `Organization` row itself deleted - which cascades every remaining
    tenant-owned row (products, versions, files, analyses, findings,
    reports, ...) away in one statement."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=grace_days)
    purged: list[uuid.UUID] = []

    orgs = db.scalars(
        select(Organization).where(
            Organization.deleted_at.is_not(None), Organization.deleted_at <= cutoff
        )
    ).all()
    for org in orgs:
        assert org.deleted_at is not None  # guaranteed by the query filter above
        for key in _org_storage_keys(db, org_id=org.id):
            storage_client.delete_object(key)
        audit.record(
            db,
            action=AuditAction.ORG_PURGED,
            actor_type=ActorType.SYSTEM,
            organization_id=org.id,
            resource_type="organization",
            resource_id=org.id,
            after={"name": org.name, "slug": org.slug, "deleted_at": org.deleted_at.isoformat()},
        )
        db.flush()
        db.delete(org)
        db.flush()
        purged.append(org.id)
    return purged


# --------------------------------------------------------------------------
# Two-phase deletion: products
# --------------------------------------------------------------------------


def request_product_deletion(
    db: Session,
    *,
    product: Product,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    ip: str | None = None,
) -> Product:
    if product.deleted_at is not None:
        raise StateInvalid("This product is already scheduled for deletion.")
    product.deleted_at = utcnow()
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_DELETE_REQUESTED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=product.organization_id,
        resource_type="product",
        resource_id=product.id,
        after={
            "deleted_at": product.deleted_at.isoformat(),
            "purge_after": (
                product.deleted_at + dt.timedelta(days=DELETION_GRACE_DAYS)
            ).isoformat(),
        },
        ip=ip,
    )
    return product


def restore_product(
    db: Session,
    *,
    product: Product,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    ip: str | None = None,
) -> Product:
    if product.deleted_at is None:
        raise StateInvalid("This product is not scheduled for deletion.")
    if utcnow() - _as_utc(product.deleted_at) > dt.timedelta(days=DELETION_GRACE_DAYS):
        raise StateInvalid(
            "The 30-day restore window has passed; this product can no longer be restored."
        )
    product.deleted_at = None
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_RESTORED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=product.organization_id,
        resource_type="product",
        resource_id=product.id,
        ip=ip,
    )
    return product


def _product_storage_keys(db: Session, *, product_id: uuid.UUID) -> list[str]:
    version_ids = list(
        db.scalars(select(ProductVersion.id).where(ProductVersion.product_id == product_id))
    )
    if not version_ids:
        return []
    keys: list[str] = list(
        db.scalars(select(File.storage_key).where(File.product_version_id.in_(version_ids)))
    )
    file_ids = list(db.scalars(select(File.id).where(File.product_version_id.in_(version_ids))))
    if file_ids:
        keys += list(db.scalars(select(FilePage.render_key).where(FilePage.file_id.in_(file_ids))))
    return sorted(set(keys))


def purge_deleted_products(
    db: Session,
    *,
    storage_client: ObjectStorageClient,
    now: dt.datetime | None = None,
    grace_days: int = DELETION_GRACE_DAYS,
) -> list[uuid.UUID]:
    """Same pattern as `purge_deleted_organizations`, scoped to one product:
    a product's reports aren't looked up here because they cascade from its
    analyses, which cascade from its versions - deleting the `Product` row
    removes them too, but their `pdf_key` objects must still be deleted from
    the object store first, so those keys are collected before the delete."""
    now = now or utcnow()
    cutoff = now - dt.timedelta(days=grace_days)
    purged: list[uuid.UUID] = []

    products = db.scalars(
        select(Product).where(Product.deleted_at.is_not(None), Product.deleted_at <= cutoff)
    ).all()
    for product in products:
        assert product.deleted_at is not None  # guaranteed by the query filter above
        for key in _product_storage_keys(db, product_id=product.id):
            storage_client.delete_object(key)
        version_ids = list(
            db.scalars(select(ProductVersion.id).where(ProductVersion.product_id == product.id))
        )
        # Reports are keyed by analysis, not directly by product/version, so
        # the only correct join to their `pdf_key` is through the analyses
        # this product's versions actually produced.
        if version_ids:
            from app.analysis.models import Analysis

            analysis_ids = list(
                db.scalars(select(Analysis.id).where(Analysis.product_version_id.in_(version_ids)))
            )
            if analysis_ids:
                pdf_keys = db.scalars(
                    select(Report.pdf_key).where(
                        Report.analysis_id.in_(analysis_ids), Report.pdf_key.is_not(None)
                    )
                )
                for pdf_key in pdf_keys:
                    if pdf_key is not None:
                        storage_client.delete_object(pdf_key)

        audit.record(
            db,
            action=AuditAction.PRODUCT_PURGED,
            actor_type=ActorType.SYSTEM,
            organization_id=product.organization_id,
            resource_type="product",
            resource_id=product.id,
            after={
                "name": product.name,
                "internal_sku": product.internal_sku,
                "deleted_at": product.deleted_at.isoformat(),
            },
        )
        db.flush()
        db.delete(product)
        db.flush()
        purged.append(product.id)
    return purged


# --------------------------------------------------------------------------
# DSR export
# --------------------------------------------------------------------------


def export_user_data(db: Session, *, user: User, org_id: uuid.UUID) -> dict[str, object]:
    """The "access/export" half of the DSR procedure IMPLEMENTATION.md 16
    calls for, scoped to the member's account within their current
    organization - see this module's own docstring for why a cross-org
    export is out of scope."""
    membership = db.scalar(
        select(Membership).where(
            Membership.user_id == user.id, Membership.organization_id == org_id
        )
    )
    audit_rows = db.scalars(
        select(AuditLog)
        .where(AuditLog.organization_id == org_id, AuditLog.actor_id == user.id)
        .order_by(AuditLog.created_at.desc())
        .limit(1000)
    ).all()

    export: dict[str, object] = {
        "exported_at": utcnow().isoformat(),
        "user": {
            "id": str(user.id),
            "email": user.email,
            "display_name": user.display_name,
            "status": user.status.value,
            "mfa_enabled": user.mfa_enabled,
            "created_at": user.created_at.isoformat(),
            "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        },
        "membership": (
            {
                "organization_id": str(membership.organization_id),
                "role": membership.role.value,
                "status": membership.status.value,
                "created_at": membership.created_at.isoformat(),
            }
            if membership is not None
            else None
        ),
        "audit_log": [
            {
                "id": str(row.id),
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "created_at": row.created_at.isoformat(),
            }
            for row in audit_rows
        ],
    }
    audit.record(
        db,
        action=AuditAction.DATA_EXPORTED,
        actor_type=ActorType.USER,
        actor_id=user.id,
        actor_label=user.email,
        organization_id=org_id,
        resource_type="user",
        resource_id=user.id,
    )
    return export
