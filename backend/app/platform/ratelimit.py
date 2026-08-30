"""Fixed-window rate limiting and login lockout counters.

Backed by an abstract key-value store so the same logic runs against Redis in
production and an in-memory store in tests.
"""

from __future__ import annotations

import time
from typing import Protocol


class CounterStore(Protocol):
    def incr(self, key: str, ttl_seconds: int) -> int: ...

    def get(self, key: str) -> int: ...

    def reset(self, key: str) -> None: ...

    def ttl(self, key: str) -> int: ...


class MemoryCounterStore:
    """Process-local counter store. Suitable for tests and single-worker local runs."""

    def __init__(self) -> None:
        self._data: dict[str, tuple[int, float]] = {}

    def _purge(self, key: str) -> None:
        entry = self._data.get(key)
        if entry and entry[1] <= time.monotonic():
            del self._data[key]

    def incr(self, key: str, ttl_seconds: int) -> int:
        self._purge(key)
        count, expires = self._data.get(key, (0, time.monotonic() + ttl_seconds))
        count += 1
        self._data[key] = (count, expires)
        return count

    def get(self, key: str) -> int:
        self._purge(key)
        return self._data.get(key, (0, 0.0))[0]

    def reset(self, key: str) -> None:
        self._data.pop(key, None)

    def ttl(self, key: str) -> int:
        self._purge(key)
        entry = self._data.get(key)
        return max(0, int(entry[1] - time.monotonic())) if entry else 0


class RedisCounterStore:
    def __init__(self, client: object) -> None:
        self._r = client

    def incr(self, key: str, ttl_seconds: int) -> int:
        pipe = self._r.pipeline()  # type: ignore[attr-defined]
        pipe.incr(key)
        pipe.expire(key, ttl_seconds, nx=True)
        count, _ = pipe.execute()
        return int(count)

    def get(self, key: str) -> int:
        value = self._r.get(key)  # type: ignore[attr-defined]
        return int(value) if value else 0

    def reset(self, key: str) -> None:
        self._r.delete(key)  # type: ignore[attr-defined]

    def ttl(self, key: str) -> int:
        return max(0, int(self._r.ttl(key)))  # type: ignore[attr-defined]


class RateLimiter:
    def __init__(self, store: CounterStore) -> None:
        self.store = store

    def hit(self, key: str, *, limit: int, window_seconds: int) -> tuple[bool, int]:
        """Register one hit. Returns (allowed, retry_after_seconds)."""
        count = self.store.incr(key, window_seconds)
        if count > limit:
            return False, self.store.ttl(key) or window_seconds
        return True, 0

    def is_locked(self, key: str, *, limit: int) -> tuple[bool, int]:
        count = self.store.get(key)
        if count >= limit:
            return True, self.store.ttl(key)
        return False, 0

    def clear(self, key: str) -> None:
        self.store.reset(key)
