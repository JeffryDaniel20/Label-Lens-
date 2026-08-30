"""Catalog domain services. Every query is tenant-scoped without exception."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.audit.models import ActorType, AuditAction
from app.catalog.models import Product, ProductVersion, ProductVersionStatus
from app.db.base import utcnow
from app.db.session import tenant_scoped
from app.platform.errors import Conflict, NotFound, StateInvalid


def get_product(db: Session, *, org_id: uuid.UUID, product_id: uuid.UUID) -> Product:
    stmt = tenant_scoped(select(Product).where(Product.id == product_id), Product, org_id)
    product: Product | None = db.scalar(stmt)
    if product is None or product.deleted_at is not None:
        # 404 rather than 403: a foreign id must not be confirmed to exist.
        raise NotFound("Product not found.")
    return product


def list_products(db: Session, *, org_id: uuid.UUID, limit: int = 50) -> list[Product]:
    stmt = tenant_scoped(select(Product), Product, org_id)
    stmt = stmt.where(Product.deleted_at.is_(None)).order_by(Product.created_at.desc()).limit(limit)
    return list(db.scalars(stmt).all())


def create_product(
    db: Session,
    *,
    org_id: uuid.UUID,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    name: str,
    internal_sku: str,
    category_hint: str | None = None,
    market_codes: list[str] | None = None,
    ip: str | None = None,
) -> Product:
    existing = db.scalar(
        tenant_scoped(
            select(Product).where(Product.internal_sku == internal_sku), Product, org_id
        )
    )
    if existing is not None:
        raise Conflict("A product with that SKU already exists in this organization.")
    product = Product(
        organization_id=org_id,
        name=name,
        internal_sku=internal_sku,
        category_hint=category_hint,
        market_codes=market_codes or [],
        created_by_user_id=actor_id if actor_type is ActorType.USER else None,
    )
    db.add(product)
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_CREATED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=org_id,
        resource_type="product",
        resource_id=product.id,
        after={"name": name, "internal_sku": internal_sku},
        ip=ip,
    )
    return product


def update_product(
    db: Session,
    *,
    org_id: uuid.UUID,
    product: Product,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    changes: dict[str, Any],
    ip: str | None = None,
) -> Product:
    before = {k: getattr(product, k) for k in changes}
    for key, value in changes.items():
        setattr(product, key, value)
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_UPDATED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=org_id,
        resource_type="product",
        resource_id=product.id,
        before=before,
        after=changes,
        ip=ip,
    )
    return product


def create_version(
    db: Session,
    *,
    org_id: uuid.UUID,
    product: Product,
    actor_id: uuid.UUID | None,
    actor_type: ActorType,
    actor_label: str | None,
    label: str = "",
    ip: str | None = None,
) -> ProductVersion:
    next_no = (
        db.scalar(
            select(func.coalesce(func.max(ProductVersion.version_no), 0)).where(
                ProductVersion.product_id == product.id,
                ProductVersion.organization_id == org_id,
            )
        )
        or 0
    ) + 1
    previous = db.scalar(
        select(ProductVersion)
        .where(
            ProductVersion.product_id == product.id,
            ProductVersion.organization_id == org_id,
            ProductVersion.superseded_at.is_(None),
        )
        .order_by(ProductVersion.version_no.desc())
    )
    if previous is not None:
        previous.superseded_at = utcnow()
        previous.status = ProductVersionStatus.SUPERSEDED

    version = ProductVersion(
        organization_id=org_id,
        product_id=product.id,
        version_no=next_no,
        label=label,
        created_by_user_id=actor_id if actor_type is ActorType.USER else None,
    )
    db.add(version)
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_VERSION_CREATED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=org_id,
        resource_type="product_version",
        resource_id=version.id,
        after={"version_no": next_no, "label": label},
        ip=ip,
    )
    return version


def get_version(
    db: Session, *, org_id: uuid.UUID, version_id: uuid.UUID
) -> ProductVersion:
    stmt = tenant_scoped(
        select(ProductVersion).where(ProductVersion.id == version_id), ProductVersion, org_id
    )
    version: ProductVersion | None = db.scalar(stmt)
    if version is None:
        raise NotFound("Product version not found.")
    return version


def update_version(
    db: Session, *, version: ProductVersion, changes: dict[str, Any]
) -> ProductVersion:
    """Mutate a version. Refused once the version is locked."""
    if version.is_locked:
        raise StateInvalid(
            "This label version is locked because an analysis depends on it. "
            "Create a new version instead."
        )
    for key, value in changes.items():
        setattr(version, key, value)
    db.flush()
    return version


def lock_version(
    db: Session,
    *,
    version: ProductVersion,
    actor_id: uuid.UUID | None = None,
    actor_type: ActorType = ActorType.SYSTEM,
    actor_label: str | None = None,
) -> ProductVersion:
    """Called by the analysis pipeline the first time a version is analysed."""
    if version.is_locked:
        return version
    version.locked_at = utcnow()
    version.status = ProductVersionStatus.LOCKED
    db.flush()
    audit.record(
        db,
        action=AuditAction.PRODUCT_VERSION_LOCKED,
        actor_type=actor_type,
        actor_id=actor_id,
        actor_label=actor_label,
        organization_id=version.organization_id,
        resource_type="product_version",
        resource_id=version.id,
        after={"locked_at": version.locked_at.isoformat()},
    )
    return version
