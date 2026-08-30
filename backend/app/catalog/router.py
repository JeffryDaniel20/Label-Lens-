"""Catalog HTTP endpoints: /v1/products and /v1/product-versions."""

from __future__ import annotations

import datetime as dt
import uuid

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog import service
from app.catalog.models import ProductVersion, ProductVersionStatus
from app.db.session import tenant_scoped
from app.identity.deps import Principal, get_db, require
from app.identity.rbac import Capability

router = APIRouter(prefix="/v1", tags=["catalog"])


class ProductCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    internal_sku: str = Field(min_length=1, max_length=80)
    category_hint: str | None = Field(default=None, max_length=80)
    market_codes: list[str] = Field(default_factory=list)


class ProductUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    category_hint: str | None = Field(default=None, max_length=80)


class ProductOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    name: str
    internal_sku: str
    category_hint: str | None
    market_codes: list[str]
    created_at: dt.datetime


class VersionCreateRequest(BaseModel):
    label: str = Field(default="", max_length=200)


class VersionUpdateRequest(BaseModel):
    label: str = Field(min_length=0, max_length=200)


class ProductVersionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: uuid.UUID
    product_id: uuid.UUID
    version_no: int
    label: str
    status: ProductVersionStatus
    locked_at: dt.datetime | None
    superseded_at: dt.datetime | None
    created_at: dt.datetime


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


@router.get("/products", response_model=list[ProductOut])
def list_products(
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> list[ProductOut]:
    products = service.list_products(db, org_id=principal.org_id)
    return [ProductOut.model_validate(p) for p in products]


@router.post("/products", response_model=ProductOut, status_code=201)
def create_product(
    payload: ProductCreateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> ProductOut:
    product = service.create_product(
        db,
        org_id=principal.org_id,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        name=payload.name,
        internal_sku=payload.internal_sku,
        category_hint=payload.category_hint,
        market_codes=payload.market_codes,
        ip=_ip(request),
    )
    return ProductOut.model_validate(product)


@router.get("/products/{product_id}", response_model=ProductOut)
def get_product(
    product_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> ProductOut:
    return ProductOut.model_validate(
        service.get_product(db, org_id=principal.org_id, product_id=product_id)
    )


@router.patch("/products/{product_id}", response_model=ProductOut)
def update_product(
    product_id: uuid.UUID,
    payload: ProductUpdateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> ProductOut:
    product = service.get_product(db, org_id=principal.org_id, product_id=product_id)
    changes = payload.model_dump(exclude_unset=True, exclude_none=True)
    if changes:
        service.update_product(
            db,
            org_id=principal.org_id,
            product=product,
            actor_id=principal.actor_id,
            actor_type=principal.actor_type,
            actor_label=principal.actor_label,
            changes=changes,
            ip=_ip(request),
        )
    return ProductOut.model_validate(product)


@router.get("/products/{product_id}/versions", response_model=list[ProductVersionOut])
def list_versions(
    product_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> list[ProductVersionOut]:
    product = service.get_product(db, org_id=principal.org_id, product_id=product_id)
    stmt = tenant_scoped(
        select(ProductVersion).where(ProductVersion.product_id == product.id),
        ProductVersion,
        principal.org_id,
    ).order_by(ProductVersion.version_no.desc())
    return [ProductVersionOut.model_validate(v) for v in db.scalars(stmt).all()]


@router.post("/products/{product_id}/versions", response_model=ProductVersionOut, status_code=201)
def create_version(
    product_id: uuid.UUID,
    payload: VersionCreateRequest,
    request: Request,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> ProductVersionOut:
    product = service.get_product(db, org_id=principal.org_id, product_id=product_id)
    version = service.create_version(
        db,
        org_id=principal.org_id,
        product=product,
        actor_id=principal.actor_id,
        actor_type=principal.actor_type,
        actor_label=principal.actor_label,
        label=payload.label,
        ip=_ip(request),
    )
    return ProductVersionOut.model_validate(version)


@router.get("/product-versions/{version_id}", response_model=ProductVersionOut)
def get_version(
    version_id: uuid.UUID,
    principal: Principal = Depends(require(Capability.PRODUCT_VIEW)),
    db: Session = Depends(get_db),
) -> ProductVersionOut:
    return ProductVersionOut.model_validate(
        service.get_version(db, org_id=principal.org_id, version_id=version_id)
    )


@router.patch("/product-versions/{version_id}", response_model=ProductVersionOut)
def update_version(
    version_id: uuid.UUID,
    payload: VersionUpdateRequest,
    principal: Principal = Depends(require(Capability.PRODUCT_MANAGE)),
    db: Session = Depends(get_db),
) -> ProductVersionOut:
    version = service.get_version(db, org_id=principal.org_id, version_id=version_id)
    service.update_version(db, version=version, changes={"label": payload.label})
    return ProductVersionOut.model_validate(version)
