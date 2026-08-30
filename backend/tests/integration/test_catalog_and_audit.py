"""Catalog CRUD, version immutability and audit-log integration tests."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.catalog import service as catalog_service
from app.catalog.models import ProductVersion
from tests.conftest import ApiSession

pytestmark = pytest.mark.integration

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}


@pytest.fixture
def owner(client) -> ApiSession:
    client.post("/v1/auth/signup", json=SIGNUP)
    return ApiSession(client, SIGNUP["email"], SIGNUP["password"])


class TestProducts:
    def test_create_get_list_update(self, owner: ApiSession) -> None:
        created = owner.post(
            "/v1/products",
            json={"name": "Masala Chips", "internal_sku": "MC-001", "market_codes": ["IN"]},
        )
        assert created.status_code == 201
        product_id = created.json()["id"]

        assert owner.get(f"/v1/products/{product_id}").json()["internal_sku"] == "MC-001"
        assert len(owner.get("/v1/products").json()) == 1

        updated = owner.patch(f"/v1/products/{product_id}", json={"name": "Masala Chips 2"})
        assert updated.json()["name"] == "Masala Chips 2"

    def test_duplicate_sku_within_org_conflicts(self, owner: ApiSession) -> None:
        payload = {"name": "A", "internal_sku": "SKU-1"}
        assert owner.post("/v1/products", json=payload).status_code == 201
        assert owner.post("/v1/products", json=payload).status_code == 409

    def test_unknown_product_is_404(self, owner: ApiSession) -> None:
        assert owner.get("/v1/products/00000000-0000-0000-0000-000000000001").status_code == 404


class TestProductVersions:
    def _product(self, owner: ApiSession) -> str:
        return owner.post(
            "/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-001"}
        ).json()["id"]

    def test_versions_increment_and_supersede(self, owner: ApiSession) -> None:
        product_id = self._product(owner)
        first = owner.post(f"/v1/products/{product_id}/versions", json={"label": "v1 artwork"})
        second = owner.post(f"/v1/products/{product_id}/versions", json={"label": "v2 artwork"})
        assert first.json()["version_no"] == 1
        assert second.json()["version_no"] == 2

        versions = owner.get(f"/v1/products/{product_id}/versions").json()
        by_no = {v["version_no"]: v for v in versions}
        assert by_no[1]["superseded_at"] is not None
        assert by_no[1]["status"] == "superseded"
        assert by_no[2]["superseded_at"] is None

    def test_locked_version_cannot_be_edited(self, owner: ApiSession, db) -> None:
        product_id = self._product(owner)
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]

        renamed = owner.patch(
            f"/v1/product-versions/{version_id}", json={"label": "renamed"}
        )
        assert renamed.status_code == 200

        version = db.scalar(
            select(ProductVersion).where(ProductVersion.id == uuid.UUID(version_id))
        )
        catalog_service.lock_version(db, version=version)
        db.commit()

        blocked = owner.patch(f"/v1/product-versions/{version_id}", json={"label": "again"})
        assert blocked.status_code == 409
        assert blocked.json()["type"].endswith("/state_invalid")

    def test_locking_is_idempotent(self, owner: ApiSession, db) -> None:
        product_id = self._product(owner)
        version_id = owner.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        version = db.scalar(
            select(ProductVersion).where(ProductVersion.id == uuid.UUID(version_id))
        )
        catalog_service.lock_version(db, version=version)
        first_lock = version.locked_at
        catalog_service.lock_version(db, version=version)
        assert version.locked_at == first_lock


class TestAuditTrail:
    def test_lifecycle_events_are_recorded(self, owner: ApiSession, db) -> None:
        owner.post("/v1/products", json={"name": "A", "internal_sku": "S1"})
        actions = {row.action for row in db.scalars(select(AuditLog)).all()}
        assert {"org.created", "auth.login.succeeded", "product.created"} <= actions

    def test_failed_login_is_audited_without_the_password(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        client.post("/v1/auth/login", json={"email": SIGNUP["email"], "password": "WrongPass123"})
        row = db.scalar(
            select(AuditLog)
            .where(AuditLog.action == "auth.login.failed")
            .order_by(AuditLog.created_at.desc())
        )
        assert row is not None
        assert row.actor_label == SIGNUP["email"]
        assert "WrongPass123" not in str(row.before) + str(row.after)

    def test_denied_access_is_audited(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        owner_session = ApiSession(client, SIGNUP["email"], SIGNUP["password"])
        owner_session.post(
            "/v1/members",
            json={"email": "viewer@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
        )
        viewer = ApiSession(client, "viewer@acmefoods.com")
        assert viewer.get("/v1/audit-logs").status_code == 403
        denied = db.scalar(select(AuditLog).where(AuditLog.action == "access.denied"))
        assert denied is not None
        assert denied.resource_id == "audit:view"

    def test_audit_endpoint_is_admin_only_and_scoped(self, owner: ApiSession) -> None:
        rows = owner.get("/v1/audit-logs").json()
        assert rows and all("correlation_id" in r for r in rows)

    def test_audit_rows_carry_the_request_correlation_id(self, owner: ApiSession, db) -> None:
        response = owner.post(
            "/v1/products",
            json={"name": "Traced", "internal_sku": "TRACE-1"},
            headers={"X-Correlation-Id": "trace-correlate-1"},
        )
        assert response.status_code == 201
        row = db.scalar(select(AuditLog).where(AuditLog.action == "product.created"))
        assert row is not None and row.correlation_id == "trace-correlate-1"
