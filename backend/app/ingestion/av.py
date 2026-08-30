"""Antivirus scanning adapter.

`ClamdAVScanner` talks to a real clamd daemon over TCP (INSTREAM), the same
protocol used in production. `NullAVScanner` is the explicit fallback when no
daemon is configured (local development without ClamAV installed): it never
claims a file is "clean" - it reports `skipped`, so the gap is visible in the
`files.av_status` column and in every audit trail rather than silently
pretending a scan happened. Wiring a real scanner in production is tracked as
a deployment prerequisite (see docs/adr and P7-T9).
"""

from __future__ import annotations

import enum
import io
from typing import Protocol


class AvVerdict(enum.StrEnum):
    CLEAN = "clean"
    INFECTED = "infected"
    SKIPPED = "skipped"
    ERROR = "error"


class AvScanner(Protocol):
    def scan(self, data: bytes) -> tuple[AvVerdict, str | None]:
        """Returns (verdict, signature_name_if_infected)."""
        ...


class NullAvScanner:
    """No antivirus daemon configured. Never claims a file is clean."""

    def scan(self, data: bytes) -> tuple[AvVerdict, str | None]:
        return AvVerdict.SKIPPED, None


class ClamdAvScanner:
    def __init__(self, *, host: str, port: int, timeout: float = 30.0) -> None:
        self._host = host
        self._port = port
        self._timeout = timeout

    def scan(self, data: bytes) -> tuple[AvVerdict, str | None]:
        import clamd

        try:
            client = clamd.ClamdNetworkSocket(
                host=self._host, port=self._port, timeout=self._timeout
            )
            result = client.instream(io.BytesIO(data))
        except (clamd.ClamdError, OSError, ConnectionError):
            return AvVerdict.ERROR, None

        status, signature = result.get("stream", ("ERROR", None))
        if status == "OK":
            return AvVerdict.CLEAN, None
        if status == "FOUND":
            return AvVerdict.INFECTED, signature
        return AvVerdict.ERROR, None


def build_av_scanner(*, host: str, port: int) -> AvScanner:
    if not host:
        return NullAvScanner()
    return ClamdAvScanner(host=host, port=port)
