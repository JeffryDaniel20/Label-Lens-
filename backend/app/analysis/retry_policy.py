"""Per-stage retry/timeout policy and exception classification (P5-T3).

Pure policy, no I/O: a stage function raises one of the two exception classes
below to say *how* its failure should be handled; `app.analysis.worker`
is what actually acts on that classification (retry, dead-letter, or fail).

- `TransientStageError` - a failure worth retrying (a provider outage, a
  network blip, a timeout): exponential backoff with jitter, up to
  `MAX_STAGE_ATTEMPTS` attempts, then dead-lettered as exhausted-but-
  still-`retryable` (a human/operator retry may still succeed later).
- `PermanentStageError` - a deterministic failure (corrupt input, a schema
  mismatch that survived repair) that must never be retried automatically -
  dead-lettered immediately as `retryable=False`.

An unclassified exception is deliberately *not* forced into either bucket
here - `app.analysis.worker` lets it propagate as P5-T2 always has, so
Arq's own generic `max_tries` still applies without this module having to
guess at every possible failure mode real stage code might someday raise.
"""

from __future__ import annotations

import datetime as dt
import random

from app.analysis.models import AnalysisState
from app.db.base import utcnow


class TransientStageError(Exception):
    """Worth retrying: a provider outage, network blip, or stage timeout."""


class PermanentStageError(Exception):
    """Deterministic and not worth retrying: corrupt input, bad schema."""


class StageTimeoutError(TransientStageError):
    """A stage exceeded its own soft timeout - treated as transient."""


MAX_STAGE_ATTEMPTS = 3

# IMPLEMENTATION.md §14: "OCR 120 s/page, extraction 90 s/call". Every other
# stage gets a conservative default until it has real, timeable work to do.
STAGE_TIMEOUTS_SECONDS: dict[AnalysisState, float] = {
    AnalysisState.OCR: 120.0,
    AnalysisState.EXTRACTING: 90.0,
}
DEFAULT_STAGE_TIMEOUT_SECONDS = 60.0

# IMPLEMENTATION.md §14: "whole analysis 20 min".
ANALYSIS_BUDGET_SECONDS = 20 * 60


def stage_timeout_seconds(state: AnalysisState) -> float:
    return STAGE_TIMEOUTS_SECONDS.get(state, DEFAULT_STAGE_TIMEOUT_SECONDS)


def backoff_seconds(attempt: int, *, rng: random.Random | None = None) -> float:
    """Exponential backoff with jitter: doubling per attempt, capped at 60s,
    plus up to 50% random jitter so a burst of simultaneously-failing jobs
    doesn't retry in lockstep."""
    uniform = rng.uniform if rng is not None else random.uniform
    base = min(2.0**attempt, 60.0)
    return base + base * uniform(0, 0.5)


def is_budget_exceeded(started_at: dt.datetime, *, now: dt.datetime | None = None) -> bool:
    now = now or utcnow()
    # SQLite (the test suite's default engine) does not actually preserve
    # timezone-awareness through `DateTime(timezone=True)` - a value read
    # back from it comes back naive. Treat a naive `started_at` as UTC
    # (every write path uses `utcnow()`/`func.now()`, both UTC) rather than
    # raising on the subtraction.
    if started_at.tzinfo is None:
        started_at = started_at.replace(tzinfo=dt.UTC)
    elapsed = (now - started_at).total_seconds()
    return elapsed > ANALYSIS_BUDGET_SECONDS
