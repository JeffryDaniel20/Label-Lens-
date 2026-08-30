"""Server-side sessions.

Sessions live in a store (Redis in production, memory in tests) so that they can
be revoked immediately - the cookie carries an opaque id and no claims.
"""

from __future__ import annotations

import datetime as dt
import json
import secrets
import uuid
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from app.db.base import utcnow
from app.identity.passwords import generate_token, hash_token

SESSION_ID_BYTES = 32


@dataclass(slots=True)
class SessionData:
    user_id: str
    org_id: str | None
    csrf_token: str
    created_at: str
    last_seen_at: str
    mfa_satisfied: bool = True
    ip: str | None = None

    def to_json(self) -> str:
        return json.dumps(asdict(self))

    @staticmethod
    def from_json(raw: str) -> SessionData:
        return SessionData(**json.loads(raw))


class SessionStore(Protocol):
    def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None: ...

    def get(self, session_id: str) -> SessionData | None: ...

    def delete(self, session_id: str) -> None: ...

    def delete_for_user(self, user_id: str) -> int: ...


class MemorySessionStore:
    def __init__(self) -> None:
        self._data: dict[str, tuple[SessionData, dt.datetime]] = {}

    def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None:
        self._data[session_id] = (data, utcnow() + dt.timedelta(seconds=ttl_seconds))

    def get(self, session_id: str) -> SessionData | None:
        entry = self._data.get(session_id)
        if entry is None:
            return None
        data, expires = entry
        if expires <= utcnow():
            del self._data[session_id]
            return None
        return data

    def delete(self, session_id: str) -> None:
        self._data.pop(session_id, None)

    def delete_for_user(self, user_id: str) -> int:
        keys = [k for k, (d, _) in self._data.items() if d.user_id == user_id]
        for key in keys:
            del self._data[key]
        return len(keys)


class RedisSessionStore:
    def __init__(self, client: Any, prefix: str = "sess:") -> None:
        self._r = client
        self._prefix = prefix

    def _key(self, session_id: str) -> str:
        return f"{self._prefix}{session_id}"

    def set(self, session_id: str, data: SessionData, ttl_seconds: int) -> None:
        self._r.setex(self._key(session_id), ttl_seconds, data.to_json())
        self._r.sadd(f"{self._prefix}user:{data.user_id}", session_id)

    def get(self, session_id: str) -> SessionData | None:
        raw = self._r.get(self._key(session_id))
        return SessionData.from_json(raw) if raw else None

    def delete(self, session_id: str) -> None:
        data = self.get(session_id)
        self._r.delete(self._key(session_id))
        if data:
            self._r.srem(f"{self._prefix}user:{data.user_id}", session_id)

    def delete_for_user(self, user_id: str) -> int:
        key = f"{self._prefix}user:{user_id}"
        ids = [i.decode() if isinstance(i, bytes) else i for i in self._r.smembers(key)]
        for session_id in ids:
            self._r.delete(self._key(session_id))
        self._r.delete(key)
        return len(ids)


class SessionManager:
    """Issues, validates and rotates sessions, applying idle and absolute expiry."""

    def __init__(self, store: SessionStore, *, idle_seconds: int, absolute_seconds: int) -> None:
        self.store = store
        self.idle_seconds = idle_seconds
        self.absolute_seconds = absolute_seconds

    def create(
        self,
        *,
        user_id: uuid.UUID,
        org_id: uuid.UUID | None,
        ip: str | None = None,
        mfa_satisfied: bool = True,
    ) -> tuple[str, SessionData]:
        session_id = secrets.token_urlsafe(SESSION_ID_BYTES)
        now = utcnow().isoformat()
        data = SessionData(
            user_id=str(user_id),
            org_id=str(org_id) if org_id else None,
            csrf_token=generate_token(16),
            created_at=now,
            last_seen_at=now,
            mfa_satisfied=mfa_satisfied,
            ip=ip,
        )
        self.store.set(session_id, data, self.idle_seconds)
        return session_id, data

    def load(self, session_id: str) -> SessionData | None:
        data = self.store.get(session_id)
        if data is None:
            return None
        created = dt.datetime.fromisoformat(data.created_at)
        if utcnow() - created > dt.timedelta(seconds=self.absolute_seconds):
            self.store.delete(session_id)
            return None
        return data

    def touch(self, session_id: str, data: SessionData) -> None:
        data.last_seen_at = utcnow().isoformat()
        self.store.set(session_id, data, self.idle_seconds)

    def set_org(self, session_id: str, data: SessionData, org_id: uuid.UUID) -> None:
        data.org_id = str(org_id)
        self.store.set(session_id, data, self.idle_seconds)

    def revoke(self, session_id: str) -> None:
        self.store.delete(session_id)

    def revoke_all_for_user(self, user_id: uuid.UUID) -> int:
        return self.store.delete_for_user(str(user_id))


def session_fingerprint(session_id: str) -> str:
    """Short, non-reversible id for logs and audit rows."""
    return hash_token(session_id)[:16]
