"""Password and opaque-token hashing.

Passwords use Argon2id with tuned parameters. API keys and recovery codes are
high-entropy random values, so they use SHA-256 (fast lookup, no brute-force
surface) rather than a memory-hard function.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.platform.config import get_settings

_MIN_PASSWORD_LENGTH = 12
# Verified against a dummy hash so a login attempt for an unknown user costs the
# same as one for a known user (no timing-based user enumeration).
_DUMMY_PASSWORD = "labellens-timing-equalisation-dummy"  # noqa: S105


def _hasher() -> PasswordHasher:
    settings = get_settings()
    return PasswordHasher(
        time_cost=settings.argon2_time_cost,
        memory_cost=settings.argon2_memory_kib,
        parallelism=settings.argon2_parallelism,
    )


def validate_password_strength(password: str) -> None:
    from app.platform.errors import ValidationFailed

    problems: list[str] = []
    if len(password) < _MIN_PASSWORD_LENGTH:
        problems.append(f"must be at least {_MIN_PASSWORD_LENGTH} characters")
    if password.lower() == password or password.upper() == password:
        problems.append("must mix upper and lower case")
    if not any(c.isdigit() for c in password):
        problems.append("must contain a digit")
    if problems:
        raise ValidationFailed(
            "Password does not meet the minimum requirements.",
            errors=[{"field": "password", "message": p} for p in problems],
        )


def hash_password(password: str) -> str:
    validate_password_strength(password)
    return _hasher().hash(password)


def verify_password(password: str, password_hash: str | None) -> bool:
    hasher = _hasher()
    if password_hash is None:
        # Spend the same work as a real verification, then fail.
        try:
            hasher.verify(hasher.hash(_DUMMY_PASSWORD), password)
        except (VerifyMismatchError, VerificationError, InvalidHashError):
            pass
        return False
    try:
        hasher.verify(password_hash, password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
    return True


def needs_rehash(password_hash: str) -> bool:
    try:
        return _hasher().check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def generate_token(nbytes: int = 32) -> str:
    return secrets.token_urlsafe(nbytes)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def verify_token(token: str, token_hash: str) -> bool:
    return hmac.compare_digest(hash_token(token), token_hash)
