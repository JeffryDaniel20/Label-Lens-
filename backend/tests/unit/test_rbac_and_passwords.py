"""Exhaustive RBAC matrix assertions and password/token hashing tests."""

from __future__ import annotations

import pytest

from app.identity.models import ROLE_RANK, Role
from app.identity.passwords import (
    generate_token,
    hash_password,
    hash_token,
    needs_rehash,
    validate_password_strength,
    verify_password,
    verify_token,
)
from app.identity.rbac import CAPABILITIES, Capability, capabilities_for, has_capability
from app.platform.errors import ValidationFailed

pytestmark = pytest.mark.unit

# The full expected matrix, written out so a permission change must be a
# deliberate edit here as well as in the source.
EXPECTED: dict[Role, set[Capability]] = {
    Role.VIEWER: {
        Capability.ORG_VIEW,
        Capability.PRODUCT_VIEW,
        Capability.ANALYSIS_VIEW,
        Capability.REPORT_VIEW,
        Capability.REPORT_GENERATE,
    },
    Role.ANALYST: {
        Capability.ORG_VIEW,
        Capability.PRODUCT_VIEW,
        Capability.ANALYSIS_VIEW,
        Capability.REPORT_VIEW,
        Capability.REPORT_GENERATE,
        Capability.PRODUCT_MANAGE,
        Capability.FILE_UPLOAD,
        Capability.ANALYSIS_RUN,
    },
    Role.REVIEWER: {
        Capability.ORG_VIEW,
        Capability.PRODUCT_VIEW,
        Capability.ANALYSIS_VIEW,
        Capability.REPORT_VIEW,
        Capability.REPORT_GENERATE,
        Capability.PRODUCT_MANAGE,
        Capability.FILE_UPLOAD,
        Capability.ANALYSIS_RUN,
        Capability.FINDING_DECIDE,
        Capability.ANALYSIS_SIGNOFF,
    },
    Role.ADMIN: {
        Capability.ORG_VIEW,
        Capability.PRODUCT_VIEW,
        Capability.ANALYSIS_VIEW,
        Capability.REPORT_VIEW,
        Capability.REPORT_GENERATE,
        Capability.PRODUCT_MANAGE,
        Capability.FILE_UPLOAD,
        Capability.ANALYSIS_RUN,
        Capability.FINDING_DECIDE,
        Capability.ANALYSIS_SIGNOFF,
        Capability.ORG_UPDATE,
        Capability.MEMBER_VIEW,
        Capability.MEMBER_MANAGE,
        Capability.APIKEY_MANAGE,
        Capability.AUDIT_VIEW,
    },
    Role.OWNER: set(Capability),
}


@pytest.mark.parametrize("role", list(Role))
@pytest.mark.parametrize("capability", list(Capability))
def test_every_role_capability_pair(role: Role, capability: Capability) -> None:
    assert has_capability(role, capability) is (capability in EXPECTED[role])


def test_every_role_has_an_entry() -> None:
    assert set(CAPABILITIES) == set(Role)


def test_capabilities_are_monotonic_with_role_rank() -> None:
    ordered = sorted(Role, key=lambda r: ROLE_RANK[r])
    for lower, higher in zip(ordered, ordered[1:], strict=False):
        assert CAPABILITIES[lower] <= CAPABILITIES[higher]


def test_capabilities_for_returns_sorted_strings() -> None:
    values = capabilities_for(Role.VIEWER)
    assert values == sorted(values)
    assert "product:view" in values


class TestPasswords:
    def test_hash_and_verify_roundtrip(self) -> None:
        hashed = hash_password("CorrectHorse42!")
        assert verify_password("CorrectHorse42!", hashed)
        assert not verify_password("wrong-password-9", hashed)

    def test_hash_is_salted(self) -> None:
        assert hash_password("CorrectHorse42!") != hash_password("CorrectHorse42!")

    @pytest.mark.parametrize(
        "weak", ["short1A", "alllowercase123", "ALLUPPERCASE123", "NoDigitsHereAtAll"]
    )
    def test_weak_passwords_rejected(self, weak: str) -> None:
        with pytest.raises(ValidationFailed):
            validate_password_strength(weak)

    def test_verify_against_missing_hash_is_false(self) -> None:
        assert verify_password("anything-at-all", None) is False

    def test_needs_rehash_on_garbage(self) -> None:
        assert needs_rehash("not-a-hash") is True


class TestTokens:
    def test_token_hash_roundtrip(self) -> None:
        token = generate_token()
        assert verify_token(token, hash_token(token))
        assert not verify_token("other", hash_token(token))

    def test_tokens_are_unique(self) -> None:
        assert len({generate_token() for _ in range(50)}) == 50
