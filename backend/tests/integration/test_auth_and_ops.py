"""Health, error-shape, signup/login/logout, MFA and API key integration tests."""

from __future__ import annotations

import pyotp
import pytest
from sqlalchemy import select

from app.identity.models import Role, User
from app.platform.config import get_settings
from tests.conftest import DEFAULT_PASSWORD, ApiSession

pytestmark = pytest.mark.integration

SIGNUP = {
    "organization_name": "Acme Foods",
    "email": "owner@acmefoods.com",
    "password": "CorrectHorse42!",
    "display_name": "Ada",
}


class TestOps:
    def test_healthz_does_not_touch_dependencies(self, client) -> None:
        assert client.get("/healthz").json() == {"status": "ok"}

    def test_readyz_reports_each_dependency(self, client) -> None:
        body = client.get("/readyz").json()
        assert body["status"] == "ready"
        assert body["checks"]["database"] == "ok"

    def test_correlation_id_is_returned_and_echoed(self, client) -> None:
        response = client.get("/healthz")
        assert len(response.headers["X-Correlation-Id"]) == 32
        echoed = client.get("/healthz", headers={"X-Correlation-Id": "trace-abc-123"})
        assert echoed.headers["X-Correlation-Id"] == "trace-abc-123"

    def test_security_headers_present(self, client) -> None:
        headers = client.get("/healthz").headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"

    def test_errors_are_problem_json_with_correlation_id(self, client) -> None:
        response = client.get("/v1/products")
        assert response.status_code == 401
        assert response.headers["content-type"].startswith("application/problem+json")
        body = response.json()
        assert body["type"].endswith("/unauthenticated")
        assert body["status"] == 401
        assert body["correlation_id"] == response.headers["X-Correlation-Id"]

    def test_validation_errors_list_offending_fields(self, client) -> None:
        response = client.post("/v1/auth/signup", json={"organization_name": "x"})
        assert response.status_code == 400
        body = response.json()
        assert body["type"].endswith("/validation_error")
        assert any("email" in e["field"] for e in body["errors"])

    def test_oversized_body_rejected(self, client) -> None:
        response = client.post(
            "/v1/auth/login",
            content=b"x" * 10,
            headers={"content-length": str(50 * 1024 * 1024), "content-type": "application/json"},
        )
        assert response.status_code == 413


class TestSignupAndLogin:
    def test_signup_creates_org_owner_and_session(self, client) -> None:
        response = client.post("/v1/auth/signup", json=SIGNUP)
        assert response.status_code == 201
        body = response.json()
        assert body["org_id"] and body["csrf_token"]
        cookie = get_settings().session_cookie_name
        assert cookie in response.cookies

        me = client.get("/v1/me", cookies=dict(response.cookies)).json()
        assert me["email"] == "owner@acmefoods.com"
        assert me["role"] == "owner"
        assert "member:manage" in me["capabilities"]

    def test_duplicate_email_conflicts(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        second = client.post("/v1/auth/signup", json={**SIGNUP, "organization_name": "Other"})
        assert second.status_code == 409

    def test_weak_password_rejected_at_signup(self, client) -> None:
        response = client.post("/v1/auth/signup", json={**SIGNUP, "password": "alllowercase"})
        assert response.status_code == 400

    def test_login_and_logout_revokes_session_server_side(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com")
        assert api.get("/v1/me").status_code == 200
        assert api.post("/v1/auth/logout").status_code == 204
        assert api.get("/v1/me").status_code == 401

    def test_lockout_after_repeated_failures(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        limit = get_settings().login_max_attempts
        for _ in range(limit):
            bad = client.post(
                "/v1/auth/login", json={"email": "owner@acmefoods.com", "password": "WrongPass123"}
            )
            assert bad.status_code == 401
        locked = client.post(
            "/v1/auth/login", json={"email": "owner@acmefoods.com", "password": DEFAULT_PASSWORD}
        )
        assert locked.status_code == 429
        assert locked.json()["type"].endswith("/account_locked")

    def test_successful_login_clears_the_failure_counter(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        client.post(
            "/v1/auth/login",
            json={"email": "owner@acmefoods.com", "password": "WrongPass123"},
        )
        ok = client.post(
            "/v1/auth/login", json={"email": "owner@acmefoods.com", "password": SIGNUP["password"]}
        )
        assert ok.status_code == 200


class TestMfa:
    def test_enrol_then_login_requires_a_code(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])

        start = api.post("/v1/me/mfa/start")
        secret = start.json()["secret"]
        assert start.json()["otpauth_uri"].startswith("otpauth://totp/")

        confirm = api.post(
            "/v1/me/mfa/confirm", json={"totp_code": pyotp.TOTP(secret).now()}
        )
        assert confirm.status_code == 200
        codes = confirm.json()["recovery_codes"]
        assert len(codes) == 8

        without_code = client.post(
            "/v1/auth/login", json={"email": "owner@acmefoods.com", "password": SIGNUP["password"]}
        )
        assert without_code.status_code == 401
        assert without_code.json()["type"].endswith("/mfa_required")

        with_code = client.post(
            "/v1/auth/login",
            json={
                "email": "owner@acmefoods.com",
                "password": SIGNUP["password"],
                "totp_code": pyotp.TOTP(secret).now(),
            },
        )
        assert with_code.status_code == 200

    def test_recovery_code_works_once(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])
        secret = api.post("/v1/me/mfa/start").json()["secret"]
        codes = api.post(
            "/v1/me/mfa/confirm", json={"totp_code": pyotp.TOTP(secret).now()}
        ).json()["recovery_codes"]

        first = client.post(
            "/v1/auth/login",
            json={
                "email": "owner@acmefoods.com",
                "password": SIGNUP["password"],
                "recovery_code": codes[0],
            },
        )
        assert first.status_code == 200
        replay = client.post(
            "/v1/auth/login",
            json={
                "email": "owner@acmefoods.com",
                "password": SIGNUP["password"],
                "recovery_code": codes[0],
            },
        )
        assert replay.status_code == 401

    def test_recovery_codes_are_stored_hashed(self, client, db) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])
        secret = api.post("/v1/me/mfa/start").json()["secret"]
        codes = api.post(
            "/v1/me/mfa/confirm", json={"totp_code": pyotp.TOTP(secret).now()}
        ).json()["recovery_codes"]
        user = db.scalar(select(User).where(User.email == "owner@acmefoods.com"))
        assert user is not None
        db.refresh(user)
        stored = {c.code_hash for c in user.recovery_codes}
        assert not stored & set(codes)


class TestApiKeys:
    def test_create_use_and_revoke(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])
        created = api.post("/v1/api-keys", json={"name": "ci", "role": "analyst"})
        assert created.status_code == 201
        plaintext = created.json()["key"]
        key_id = created.json()["api_key"]["id"]

        listed = api.get("/v1/api-keys").json()
        assert listed[0]["prefix"] in plaintext
        assert "key_hash" not in listed[0]

        headers = {"Authorization": f"Bearer {plaintext}"}
        assert client.get("/v1/products", headers=headers).status_code == 200

        assert api.delete(f"/v1/api-keys/{key_id}").status_code == 204
        assert client.get("/v1/products", headers=headers).status_code == 401

    def test_api_key_cannot_be_granted_admin(self, client) -> None:
        client.post("/v1/auth/signup", json=SIGNUP)
        api = ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])
        response = api.post("/v1/api-keys", json={"name": "bad", "role": "admin"})
        assert response.status_code == 403

    def test_malformed_and_unknown_keys_rejected(self, client) -> None:
        for value in ("garbage", "llk_dead.beef"):
            response = client.get("/v1/products", headers={"Authorization": f"Bearer {value}"})
            assert response.status_code == 401


class TestMembers:
    def _owner(self, client) -> ApiSession:
        client.post("/v1/auth/signup", json=SIGNUP)
        return ApiSession(client, "owner@acmefoods.com", SIGNUP["password"])

    def test_add_list_and_promote_member(self, client) -> None:
        owner = self._owner(client)
        created = owner.post(
            "/v1/members",
            json={
                "email": "analyst@acmefoods.com",
                "role": "analyst",
                "password": DEFAULT_PASSWORD,
            },
        )
        assert created.status_code == 201
        membership_id = created.json()["membership_id"]
        assert len(owner.get("/v1/members").json()) == 2

        promoted = owner.patch(f"/v1/members/{membership_id}", json={"role": "reviewer"})
        assert promoted.json()["role"] == "reviewer"

    def test_last_owner_cannot_be_demoted_or_removed(self, client) -> None:
        owner = self._owner(client)
        members = owner.get("/v1/members").json()
        owner_membership = next(m for m in members if m["role"] == "owner")
        demote = owner.patch(
            f"/v1/members/{owner_membership['membership_id']}", json={"role": "viewer"}
        )
        assert demote.status_code == 409
        removed = owner.delete(f"/v1/members/{owner_membership['membership_id']}")
        assert removed.status_code == 409

    def test_admin_cannot_grant_owner(self, client) -> None:
        owner = self._owner(client)
        owner.post(
            "/v1/members",
            json={"email": "admin@acmefoods.com", "role": "admin", "password": DEFAULT_PASSWORD},
        )
        admin = ApiSession(client, "admin@acmefoods.com")
        response = admin.post(
            "/v1/members",
            json={"email": "x@acmefoods.com", "role": "owner", "password": DEFAULT_PASSWORD},
        )
        assert response.status_code == 403

    def test_duplicate_membership_conflicts(self, client) -> None:
        owner = self._owner(client)
        payload = {
            "email": "dupe@acmefoods.com",
            "role": Role.ANALYST.value,
            "password": DEFAULT_PASSWORD,
        }
        assert owner.post("/v1/members", json=payload).status_code == 201
        assert owner.post("/v1/members", json=payload).status_code == 409
