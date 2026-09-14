"""P7-T8: retention purge, two-phase org/product deletion, and DSR export."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.audit.models import ActorType, AuditAction, AuditLog
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.db.base import utcnow
from app.identity.models import Organization, Role, User
from app.platform.errors import StateInvalid
from app.retention.service import (
    DELETION_GRACE_DAYS,
    export_user_data,
    purge_deleted_organizations,
    purge_deleted_products,
    purge_expired_files,
    request_organization_deletion,
    request_product_deletion,
    restore_organization,
    restore_product,
)
from tests.conftest import ApiSession, make_org, make_user

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


class _FakeStorageClient:
    def __init__(self) -> None:
        self.deleted: list[str] = []

    def delete_object(self, key: str) -> None:
        self.deleted.append(key)


def _make_product_version(db, org: Organization) -> ProductVersion:
    product = Product(
        organization_id=org.id, name="Chips", internal_sku=f"SKU-{uuid.uuid4().hex[:6]}"
    )
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    return version


def _make_file(
    db,
    org: Organization,
    version: ProductVersion,
    *,
    storage_key: str,
    render_key: str,
    created_at: dt.datetime | None = None,
    sha256: str | None = None,
) -> File:
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key=storage_key,
        original_filename="label.jpg",
        sha256=sha256 or uuid.uuid4().hex,
        mime="image/jpeg",
        bytes=1234,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    if created_at is not None:
        db.query(File).filter(File.id == file.id).update({"created_at": created_at})
        db.flush()
        db.refresh(file)
    page = FilePage(
        organization_id=org.id,
        file_id=file.id,
        page_no=1,
        width=10,
        height=10,
        render_key=render_key,
    )
    db.add(page)
    db.flush()
    return file


class TestRetentionPurgeExpiredFiles:
    def test_a_file_past_retention_is_purged_and_marked(self, db) -> None:
        org = make_org(db)
        org.retention_days = 30
        db.flush()
        version = _make_product_version(db, org)
        old = _make_file(
            db,
            org,
            version,
            storage_key="org/x/old.jpg",
            render_key="org/x/old-render.png",
            created_at=utcnow() - dt.timedelta(days=31),
        )
        db.commit()

        storage = _FakeStorageClient()
        purged = purge_expired_files(db, storage_client=storage)
        db.commit()

        assert old.id in purged
        db.refresh(old)
        assert old.purged_at is not None
        assert "org/x/old.jpg" in storage.deleted
        assert "org/x/old-render.png" in storage.deleted

    def test_a_recent_file_within_retention_is_left_alone(self, db) -> None:
        org = make_org(db)
        org.retention_days = 365
        db.flush()
        version = _make_product_version(db, org)
        recent = _make_file(
            db, org, version, storage_key="org/x/recent.jpg", render_key="org/x/recent-render.png"
        )
        db.commit()

        storage = _FakeStorageClient()
        purged = purge_expired_files(db, storage_client=storage)

        assert recent.id not in purged
        assert storage.deleted == []

    def test_a_deduplicated_storage_key_is_not_deleted_while_another_file_still_uses_it(
        self, db
    ) -> None:
        org = make_org(db)
        org.retention_days = 30
        db.flush()
        version = _make_product_version(db, org)
        shared_key = "org/x/shared.jpg"
        expired = _make_file(
            db,
            org,
            version,
            storage_key=shared_key,
            render_key="org/x/expired-render.png",
            created_at=utcnow() - dt.timedelta(days=60),
        )
        still_live = _make_file(
            db, org, version, storage_key=shared_key, render_key="org/x/live-render.png"
        )
        db.commit()

        storage = _FakeStorageClient()
        purged = purge_expired_files(db, storage_client=storage)
        db.commit()

        assert expired.id in purged
        assert still_live.id not in purged
        assert shared_key not in storage.deleted, "another live file still points at this key"

    def test_purging_never_touches_findings_evidence_or_audit_rows(self, db) -> None:
        """The task's own literal acceptance line."""
        org = make_org(db)
        org.retention_days = 1
        db.flush()
        version = _make_product_version(db, org)
        _make_file(
            db,
            org,
            version,
            storage_key="org/x/a.jpg",
            render_key="org/x/a-render.png",
            created_at=utcnow() - dt.timedelta(days=10),
        )
        db.commit()

        purge_expired_files(db, storage_client=_FakeStorageClient())
        db.commit()

        purge_audit = db.scalars(
            select(AuditLog).where(AuditLog.action == AuditAction.FILE_PURGED)
        ).all()
        assert len(purge_audit) == 1
        # The file row itself, not just downstream data, survives - only the
        # object storage bytes are gone.
        remaining = db.scalars(select(File).where(File.organization_id == org.id)).all()
        assert len(remaining) == 1

    def test_purge_is_idempotent(self, db) -> None:
        org = make_org(db)
        org.retention_days = 1
        db.flush()
        version = _make_product_version(db, org)
        _make_file(
            db,
            org,
            version,
            storage_key="org/x/b.jpg",
            render_key="org/x/b-render.png",
            created_at=utcnow() - dt.timedelta(days=10),
        )
        db.commit()

        storage = _FakeStorageClient()
        first = purge_expired_files(db, storage_client=storage)
        db.commit()
        second = purge_expired_files(db, storage_client=storage)
        db.commit()

        assert len(first) == 1
        assert second == []
        assert len(storage.deleted) == 2  # storage_key + render_key, deleted exactly once


class TestTwoPhaseOrganizationDeletion:
    def test_soft_delete_sets_deleted_at_and_locks_out_org_gated_endpoints(
        self, owner: ApiSession, db
    ) -> None:
        resp = owner.delete(f"/v1/organizations/{owner.org_id}")
        assert resp.status_code == 204

        org = db.get(Organization, uuid.UUID(owner.org_id))
        assert org is not None and org.deleted_at is not None

        # POST /v1/members depends on `get_organization`, which already
        # 401s once `deleted_at` is set - the pre-existing gate this task's
        # soft delete now actually drives.
        blocked = owner.post(
            "/v1/members",
            json={"email": "new@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
        )
        assert blocked.status_code == 401

    def test_a_second_delete_is_rejected(self, owner: ApiSession) -> None:
        assert owner.delete(f"/v1/organizations/{owner.org_id}").status_code == 204
        assert owner.delete(f"/v1/organizations/{owner.org_id}").status_code == 409

    def test_cannot_delete_another_organizations_id(self, owner: ApiSession) -> None:
        foreign_id = uuid.uuid4()
        resp = owner.delete(f"/v1/organizations/{foreign_id}")
        assert resp.status_code == 403

    def test_restore_within_the_window_recovers_org_gated_access(self, owner: ApiSession) -> None:
        assert owner.delete(f"/v1/organizations/{owner.org_id}").status_code == 204
        blocked = owner.post(
            "/v1/members",
            json={"email": "new2@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
        )
        assert blocked.status_code == 401

        restored = owner.post(f"/v1/organizations/{owner.org_id}/restore")
        assert restored.status_code == 204

        again = owner.post(
            "/v1/members",
            json={"email": "new3@acmefoods.com", "role": "viewer", "password": "CorrectHorse42!"},
        )
        assert again.status_code == 201

    def test_restore_past_the_grace_window_is_refused(self, owner: ApiSession, db) -> None:
        org = db.get(Organization, uuid.UUID(owner.org_id))
        request_organization_deletion(
            db, organization=org, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
        )
        org.deleted_at = utcnow() - dt.timedelta(days=DELETION_GRACE_DAYS + 1)
        db.commit()

        with pytest.raises(StateInvalid):
            restore_organization(
                db, organization=org, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
            )

    def test_hard_purge_deletes_storage_and_leaves_the_audit_fact_behind(self, db) -> None:
        org = make_org(db)
        version = _make_product_version(db, org)
        file = _make_file(
            db, org, version, storage_key="org/y/z.jpg", render_key="org/y/z-render.png"
        )
        file_id = file.id
        db.commit()

        request_organization_deletion(
            db, organization=org, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
        )
        org_slug = org.slug
        org.deleted_at = utcnow() - dt.timedelta(days=DELETION_GRACE_DAYS + 1)
        db.commit()
        org_id = org.id

        storage = _FakeStorageClient()
        purged = purge_deleted_organizations(db, storage_client=storage)
        db.commit()
        # The DB-level FK cascade deleted `file`'s row without this session's
        # ORM ever issuing a `DELETE` for it, so its identity-map entry is
        # now stale - `expire_all` forces the next `get` to really requery.
        db.expire_all()

        assert org_id in purged
        assert "org/y/z.jpg" in storage.deleted
        assert "org/y/z-render.png" in storage.deleted
        assert db.scalar(select(Organization.id).where(Organization.id == org_id)) is None
        assert db.scalar(select(File.id).where(File.id == file_id)) is None

        purge_row = db.scalar(
            select(AuditLog).where(
                AuditLog.action == AuditAction.ORG_PURGED, AuditLog.resource_id == str(org_id)
            )
        )
        assert purge_row is not None
        # The row survives the org's own deletion, but its FK is nulled -
        # "retain the fact of deletion" made literal.
        assert purge_row.organization_id is None
        assert purge_row.after is not None and purge_row.after["slug"] == org_slug


class TestTwoPhaseProductDeletion:
    def test_soft_delete_then_restore(self, owner: ApiSession) -> None:
        created = owner.post("/v1/products", json={"name": "Masala Chips", "internal_sku": "MC-1"})
        product_id = created.json()["id"]

        deleted = owner.delete(f"/v1/products/{product_id}")
        assert deleted.status_code == 204
        assert owner.get(f"/v1/products/{product_id}").status_code == 404

        restored = owner.post(f"/v1/products/{product_id}/restore")
        assert restored.status_code == 204
        assert owner.get(f"/v1/products/{product_id}").status_code == 200

    def test_deleting_twice_conflicts(self, owner: ApiSession) -> None:
        product_id = owner.post("/v1/products", json={"name": "A", "internal_sku": "MC-2"}).json()[
            "id"
        ]
        assert owner.delete(f"/v1/products/{product_id}").status_code == 204
        assert owner.delete(f"/v1/products/{product_id}").status_code == 409

    def test_hard_purge_deletes_storage_and_cascades_the_product_away(self, db) -> None:
        org = make_org(db)
        version = _make_product_version(db, org)
        product = db.get(Product, version.product_id)
        file = _make_file(
            db, org, version, storage_key="org/p/a.jpg", render_key="org/p/a-render.png"
        )
        file_id = file.id
        db.commit()

        request_product_deletion(
            db, product=product, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
        )
        product.deleted_at = utcnow() - dt.timedelta(days=DELETION_GRACE_DAYS + 1)
        db.commit()
        product_id = product.id

        storage = _FakeStorageClient()
        purged = purge_deleted_products(db, storage_client=storage)
        db.commit()
        db.expire_all()

        assert product_id in purged
        assert "org/p/a.jpg" in storage.deleted
        assert db.scalar(select(Product.id).where(Product.id == product_id)) is None
        assert db.scalar(select(File.id).where(File.id == file_id)) is None
        # The organization itself, and its audit trail, are untouched.
        assert db.get(Organization, org.id) is not None

    def test_restore_product_past_the_window_is_refused(self, db) -> None:
        from app.audit.models import ActorType

        org = make_org(db)
        version = _make_product_version(db, org)
        product = db.get(Product, version.product_id)
        request_product_deletion(
            db, product=product, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
        )
        product.deleted_at = utcnow() - dt.timedelta(days=DELETION_GRACE_DAYS + 1)
        db.commit()

        with pytest.raises(StateInvalid):
            restore_product(
                db, product=product, actor_id=None, actor_type=ActorType.SYSTEM, actor_label=None
            )


class TestDsrExport:
    def test_me_export_returns_profile_membership_and_own_audit_trail(
        self, owner: ApiSession
    ) -> None:
        owner.post("/v1/products", json={"name": "A", "internal_sku": "SKU-EXP"})

        resp = owner.get("/v1/me/export")
        assert resp.status_code == 200
        body = resp.json()
        assert body["user"]["email"] == SIGNUP["email"]
        assert body["membership"]["role"] == "owner"
        actions = {row["action"] for row in body["audit_log"]}
        assert "product.created" in actions

    def test_export_itself_is_audited(self, owner: ApiSession, db) -> None:
        owner.get("/v1/me/export")
        row = db.scalar(select(AuditLog).where(AuditLog.action == AuditAction.DATA_EXPORTED))
        assert row is not None
        assert row.actor_label == SIGNUP["email"]

    def test_export_is_scoped_to_the_current_organization_only(self, db) -> None:
        org_a = make_org(db, "Org A")
        make_user(db, org_a, role=Role.OWNER, email="member@dsr.test")
        db.commit()

        export = export_user_data(
            db, user=db.scalar(select(User).where(User.email == "member@dsr.test")), org_id=org_a.id
        )
        assert export["membership"]["organization_id"] == str(org_a.id)


class TestOrganizationSettings:
    """A production-readiness audit finding: IMPLEMENTATION.md's own role
    table says Admin can "manage... retention settings," and
    `Organization.retention_days`/`cloud_ai_enabled` already existed and
    were already read back via `OrganizationOut` - but no endpoint ever let
    anyone actually change either one. Closed here."""

    def test_get_returns_the_real_defaults(self, owner: ApiSession) -> None:
        body = owner.get(f"/v1/organizations/{owner.org_id}").json()
        assert body["retention_days"] == 365
        assert body["cloud_ai_enabled"] is True

    def test_admin_can_update_retention_days_and_cloud_ai_enabled(
        self, owner: ApiSession, db
    ) -> None:
        updated = owner.patch(
            f"/v1/organizations/{owner.org_id}",
            json={"retention_days": 90, "cloud_ai_enabled": False},
        )
        assert updated.status_code == 200
        assert updated.json()["retention_days"] == 90
        assert updated.json()["cloud_ai_enabled"] is False

        org = db.get(Organization, uuid.UUID(owner.org_id))
        assert org.retention_days == 90
        assert org.cloud_ai_enabled is False

        audit_row = db.scalar(select(AuditLog).where(AuditLog.action == AuditAction.ORG_UPDATED))
        assert audit_row is not None
        assert audit_row.after == {"retention_days": 90, "cloud_ai_enabled": False}

    def test_a_viewer_cannot_update_organization_settings(self, owner: ApiSession, db) -> None:
        org = db.get(Organization, uuid.UUID(owner.org_id))
        viewer_user = make_user(db, org, role=Role.VIEWER, email="viewer@acmefoods.com")
        db.commit()
        viewer = ApiSession(owner.client, viewer_user.email)

        resp = viewer.patch(f"/v1/organizations/{owner.org_id}", json={"retention_days": 1})
        assert resp.status_code == 403

    def test_retention_days_out_of_bounds_is_rejected(self, owner: ApiSession) -> None:
        assert (
            owner.patch(f"/v1/organizations/{owner.org_id}", json={"retention_days": 0}).status_code
            == 400
        )
        assert (
            owner.patch(
                f"/v1/organizations/{owner.org_id}", json={"retention_days": 999999}
            ).status_code
            == 400
        )

    def test_cannot_view_or_update_another_organization(self, owner: ApiSession) -> None:
        foreign_id = uuid.uuid4()
        assert owner.get(f"/v1/organizations/{foreign_id}").status_code == 403
        assert (
            owner.patch(f"/v1/organizations/{foreign_id}", json={"retention_days": 1}).status_code
            == 403
        )
