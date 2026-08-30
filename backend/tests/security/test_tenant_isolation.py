"""Tenant isolation, CSRF, enumeration and session-revocation tests.

This suite is a required CI gate: a failure here means cross-tenant exposure.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from app.catalog.models import Product
from app.db.session import tenant_scoped
from tests.conftest import DEFAULT_PASSWORD, ApiSession

pytestmark = pytest.mark.security

ORG_A = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
}
ORG_B = {
    "organization_name": "Beta Foods",
    "email": "owner@betafoods.com",
    "password": "CorrectHorse42!",
}


@pytest.fixture
def two_orgs(client):
    client.post("/v1/auth/signup", json=ORG_A)
    client.post("/v1/auth/signup", json=ORG_B)
    a = ApiSession(client, ORG_A["email"], ORG_A["password"])
    b = ApiSession(client, ORG_B["email"], ORG_B["password"])
    return a, b


class TestCrossTenantResourceAccess:
    def test_product_of_another_org_is_not_found(self, two_orgs) -> None:
        a, b = two_orgs
        product_id = a.post(
            "/v1/products", json={"name": "Secret", "internal_sku": "S-1"}
        ).json()["id"]
        # 404, not 403: existence must not be confirmed to an outsider.
        assert b.get(f"/v1/products/{product_id}").status_code == 404
        assert b.patch(f"/v1/products/{product_id}", json={"name": "hijack"}).status_code == 404
        assert b.get(f"/v1/products/{product_id}/versions").status_code == 404
        assert b.post(f"/v1/products/{product_id}/versions", json={}).status_code == 404

    def test_product_version_of_another_org_is_not_found(self, two_orgs) -> None:
        a, b = two_orgs
        product_id = a.post("/v1/products", json={"name": "S", "internal_sku": "S-2"}).json()["id"]
        version_id = a.post(f"/v1/products/{product_id}/versions", json={}).json()["id"]
        assert b.get(f"/v1/product-versions/{version_id}").status_code == 404
        assert b.patch(f"/v1/product-versions/{version_id}", json={"label": "x"}).status_code == 404

    def test_listing_never_leaks_another_orgs_rows(self, two_orgs) -> None:
        a, b = two_orgs
        a.post("/v1/products", json={"name": "A only", "internal_sku": "A-1"})
        assert b.get("/v1/products").json() == []
        assert [p["name"] for p in a.get("/v1/products").json()] == ["A only"]

    def test_members_and_audit_are_scoped(self, two_orgs) -> None:
        a, b = two_orgs
        assert {m["email"] for m in a.get("/v1/members").json()} == {ORG_A["email"]}
        assert {m["email"] for m in b.get("/v1/members").json()} == {ORG_B["email"]}
        b_orgs = {row["actor_label"] for row in b.get("/v1/audit-logs").json()}
        assert ORG_A["email"] not in b_orgs

    def test_api_key_of_another_org_cannot_be_revoked(self, two_orgs, client) -> None:
        a, b = two_orgs
        key_id = a.post("/v1/api-keys", json={"name": "k"}).json()["api_key"]["id"]
        assert b.delete(f"/v1/api-keys/{key_id}").status_code == 404

    def test_api_key_sees_only_its_own_org(self, two_orgs, client) -> None:
        a, b = two_orgs
        a.post("/v1/products", json={"name": "A only", "internal_sku": "A-9"})
        key = a.post("/v1/api-keys", json={"name": "k"}).json()["key"]
        b.post("/v1/products", json={"name": "B only", "internal_sku": "B-9"})
        listed = client.get("/v1/products", headers={"Authorization": f"Bearer {key}"}).json()
        assert [p["name"] for p in listed] == ["A only"]

    def test_membership_of_another_org_cannot_be_modified(self, two_orgs) -> None:
        a, b = two_orgs
        membership_id = a.get("/v1/members").json()[0]["membership_id"]
        assert b.patch(f"/v1/members/{membership_id}", json={"role": "viewer"}).status_code == 404
        assert b.delete(f"/v1/members/{membership_id}").status_code == 404


class TestApplicationScopingHelper:
    def test_tenant_scoped_requires_a_tenant_column(self, db) -> None:
        from app.identity.models import User

        with pytest.raises(TypeError):
            tenant_scoped(select(User), User, uuid.uuid4())

    def test_tenant_scoped_filters_rows(self, client, db) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        client.post("/v1/auth/signup", json=ORG_B)
        a = ApiSession(client, ORG_A["email"], ORG_A["password"])
        a.post("/v1/products", json={"name": "A", "internal_sku": "A-1"})
        other_org = uuid.uuid4()
        rows = db.scalars(tenant_scoped(select(Product), Product, other_org)).all()
        assert rows == []


class TestAuthorizationSurface:
    @pytest.mark.parametrize(
        ("method", "path"),
        [
            ("GET", "/v1/me"),
            ("GET", "/v1/products"),
            ("POST", "/v1/products"),
            ("GET", "/v1/members"),
            ("POST", "/v1/members"),
            ("GET", "/v1/api-keys"),
            ("POST", "/v1/api-keys"),
            ("GET", "/v1/audit-logs"),
        ],
    )
    def test_every_endpoint_requires_authentication(self, client, method: str, path: str) -> None:
        assert client.request(method, path, json={}).status_code == 401

    def test_viewer_is_denied_write_capabilities(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        owner = ApiSession(client, ORG_A["email"], ORG_A["password"])
        owner.post(
            "/v1/members",
            json={
                "email": "viewer@acmefoods.com",
                "role": "viewer",
                "password": DEFAULT_PASSWORD,
            },
        )
        viewer = ApiSession(client, "viewer@acmefoods.com")
        assert viewer.get("/v1/products").status_code == 200
        denied = viewer.post("/v1/products", json={"name": "x", "internal_sku": "x"})
        assert denied.status_code == 403
        assert viewer.get("/v1/members").status_code == 403
        assert viewer.get("/v1/api-keys").status_code == 403

    def test_analyst_cannot_manage_members_or_keys(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        owner = ApiSession(client, ORG_A["email"], ORG_A["password"])
        owner.post(
            "/v1/members",
            json={
                "email": "analyst@acmefoods.com",
                "role": "analyst",
                "password": DEFAULT_PASSWORD,
            },
        )
        analyst = ApiSession(client, "analyst@acmefoods.com")
        created = analyst.post("/v1/products", json={"name": "p", "internal_sku": "p1"})
        assert created.status_code == 201
        assert analyst.post("/v1/api-keys", json={"name": "k"}).status_code == 403


class TestSessionSecurity:
    def test_state_changing_request_without_csrf_token_is_rejected(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        session = ApiSession(client, ORG_A["email"], ORG_A["password"])
        response = client.post(
            "/v1/products",
            json={"name": "x", "internal_sku": "x1"},
            cookies=session.cookies,
        )
        assert response.status_code == 403

    def test_wrong_csrf_token_is_rejected(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        session = ApiSession(client, ORG_A["email"], ORG_A["password"])
        response = client.post(
            "/v1/products",
            json={"name": "x", "internal_sku": "x1"},
            cookies=session.cookies,
            headers={"X-CSRF-Token": "not-the-right-token"},
        )
        assert response.status_code == 403

    def test_session_cookie_is_httponly_and_samesite(self, client) -> None:
        response = client.post("/v1/auth/signup", json=ORG_A)
        set_cookie = response.headers["set-cookie"].lower()
        assert "httponly" in set_cookie
        assert "samesite=lax" in set_cookie

    def test_forged_session_cookie_is_rejected(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        response = client.get("/v1/me", cookies={"ll_session": "forged-session-id"})
        assert response.status_code == 401

    def test_membership_removal_invalidates_the_session(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        owner = ApiSession(client, ORG_A["email"], ORG_A["password"])
        created = owner.post(
            "/v1/members",
            json={"email": "temp@acmefoods.com", "role": "analyst", "password": DEFAULT_PASSWORD},
        ).json()
        temp = ApiSession(client, "temp@acmefoods.com")
        assert temp.get("/v1/me").status_code == 200
        owner.delete(f"/v1/members/{created['membership_id']}")
        assert temp.get("/v1/me").status_code == 401


class TestEnumeration:
    def test_unknown_and_known_emails_fail_identically(self, client) -> None:
        client.post("/v1/auth/signup", json=ORG_A)
        unknown = client.post(
            "/v1/auth/login",
            json={"email": "nobody@nowhere-unknown.com", "password": "WrongPass123"},
        )
        known = client.post(
            "/v1/auth/login", json={"email": ORG_A["email"], "password": "WrongPass123"}
        )
        assert unknown.status_code == known.status_code == 401
        assert unknown.json()["detail"] == known.json()["detail"]
        assert unknown.json()["type"] == known.json()["type"]
