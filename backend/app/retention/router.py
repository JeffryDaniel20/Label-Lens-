"""Retention/deletion/DSR HTTP endpoints (P7-T8):

- `GET`/`PATCH /v1/organizations/{id}` - reading and updating an org's own
  settings (`retention_days`/`cloud_ai_enabled`). Added in a later
  production-readiness audit: Admin's own "manage... retention settings"
  capability (IMPLEMENTATION.md's role table) had no endpoint to actually
  exercise it - `OrganizationOut` existed and the columns existed, but
  nothing ever wrote to them.
- `DELETE`/`POST .../restore` on organizations and products - phase one
  (soft delete, 30-day restore window) of the two-phase deletion IMPLEMENTATION.md
  16 calls for. Phase two (the hard purge) is a maintenance job
  (`scripts/purge_retention.py`), never an HTTP action - nothing should be
  able to trigger an irreversible, cross-tenant-cascading delete from a
  single request.
- `GET /v1/me/export` - the DSR "access/export" endpoint.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import Product
from app.db.session import tenant_scoped
from app.identity import schemas
from app.identity import service as identity_service
from app.identity.deps import Principal, get_db, get_organization, require
from app.identity.models import Organization
from app.identity.rbac import Capability
from app.platform.errors import Forbidden, NotFound, Unauthenticated
from app.retention import service as retention_service

router = APIRouter(prefix="/v1", tags=["retention"])


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _lookup_organization_ignoring_deletion(db: Session, *, org_id: uuid.UUID) -> Organization:
    """`app.identity.deps.get_organization` 401s once `deleted_at` is set -
    correct for every ordinary request, but these two endpoints are exactly
    the ones that need to see an already-deleted org, to tell "already
    deleted" (409) from "never existed" and to restore it at all."""
    org = db.get(Organization, org_id)
    if org is None:
        raise Unauthenticated("Organization is unavailable.")
    return org


def _lookup_product_ignoring_deletion(
    db: Session, *, org_id: uuid.UUID, product_id: uuid.UUID
) -> Product:
    """`catalog_service.get_product` 404s on an already-soft-deleted product
    (it filters `deleted_at.is_(None)`, correctly, for every ordinary read) -
    the two endpoints below need to see it anyway, to tell "already deleted"
    (409) from "never existed" (404) and to restore it at all."""
    stmt = tenant_scoped(select(Product).where(Product.id == product_id), Product, org_id)
    product: Product | None = db.scalar(stmt)
    if product is None:
        raise NotFound("Product not found.")
    return product


@router.get("/organizations/{org_id}", response_model=schemas.OrganizationOut)
def get_organization_settings(
    org_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.ORG_VIEW)),
    org: Organization = Depends(get_organization),
) -> schemas.OrganizationOut:
    if org_id != principal.org_id:
        raise Forbidden("You may only view the organization you belong to.")
    return schemas.OrganizationOut.model_validate(org)


@router.patch("/organizations/{org_id}", response_model=schemas.OrganizationOut)
def update_organization_settings(
    org_id: uuid.UUID,
    payload: schemas.OrganizationUpdateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.ORG_UPDATE)),
    org: Organization = Depends(get_organization),
    db: Session = Depends(get_db),
) -> schemas.OrganizationOut:
    if org_id != principal.org_id:
        raise Forbidden("You may only update the organization you belong to.")
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if changes:
        identity_service.update_organization(
            db,
            org=org,
            actor_id=principal.actor_id,
            actor_type=principal.actor_type,
            actor_label=principal.actor_label,
            changes=changes,
            ip=_ip(request),
        )
        db.commit()
    return schemas.OrganizationOut.model_validate(org)


@router.delete("/organizations/{org_id}", status_code=204)
def delete_organization(
    org_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ORG_DELETE)),
    db: Session = Depends(get_db),
) -> None:
    if org_id != principal.org_id:
        # Checked before any lookup, and regardless of whether org_id
        # happens to exist: a caller is only ever bound to one org
        # (`Principal.org_id`), so a mismatched path id is either a typo or
        # someone probing for another tenant's id, and 403 is correct
        # either way without confirming or denying the other id exists.
        raise Forbidden("You may only delete the organization you belong to.")
    org = _lookup_organization_ignoring_deletion(db, org_id=org_id)
    retention_service.request_organization_deletion(
        db,
        organization=org,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        ip=_ip(request),
    )
    db.commit()


@router.post("/organizations/{org_id}/restore", status_code=204)
def restore_organization(
    org_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.ORG_DELETE)),
    db: Session = Depends(get_db),
) -> None:
    if org_id != principal.org_id:
        raise Forbidden("You may only restore the organization you belong to.")
    org = _lookup_organization_ignoring_deletion(db, org_id=org_id)
    retention_service.restore_organization(
        db,
        organization=org,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        ip=_ip(request),
    )
    db.commit()


@router.delete("/products/{product_id}", status_code=204)
def delete_product(
    product_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> None:
    product = _lookup_product_ignoring_deletion(db, org_id=principal.org_id, product_id=product_id)
    retention_service.request_product_deletion(
        db,
        product=product,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        ip=_ip(request),
    )
    db.commit()


@router.post("/products/{product_id}/restore", status_code=204)
def restore_product(
    product_id: uuid.UUID,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> None:
    product = _lookup_product_ignoring_deletion(db, org_id=principal.org_id, product_id=product_id)
    retention_service.restore_product(
        db,
        product=product,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        ip=_ip(request),
    )
    db.commit()


@router.get("/me/export")
def export_my_data(
    principal: Principal = Depends(require(Capability.ORG_VIEW)),
    db: Session = Depends(get_db),
) -> dict[str, object]:
    if principal.user is None:
        raise Forbidden("Data export is only available to interactive (non-API-key) sessions.")
    export = retention_service.export_user_data(db, user=principal.user, org_id=principal.org_id)
    db.commit()
    return export
