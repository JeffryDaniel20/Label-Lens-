"""Live progress streaming over Server-Sent Events (P5-T5).

IMPLEMENTATION.md §17 is explicit that SSE is "optional - polling is fine
initially," so this is deliberately the simplest thing that satisfies the
acceptance criterion ("a running analysis reports live stage and
percentage"): poll `analysis_events` on an interval and re-emit whatever is
new, closing the stream once the analysis reaches a stopping state. No
pub/sub, no Redis channel, no long-lived DB listener - `analysis_events` is
already the durable source of truth (P5-T1), so polling it is sufficient
and, unlike a message broker, can never miss an event a stage genuinely
committed.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator

from sqlalchemy.orm import Session

from app.analysis import service
from app.analysis.models import AnalysisEvent
from app.analysis.stages import STOPPING_STATES, progress_percentage, progress_percentage_for_state

DEFAULT_POLL_INTERVAL_SECONDS = 0.5


def _json_default(value: object) -> str:
    if isinstance(value, dt.datetime):
        return value.isoformat()
    raise TypeError(f"Not JSON serializable: {value!r}")


def format_sse_event(event: AnalysisEvent, *, percentage: int) -> str:
    payload = {
        "sequence": event.sequence,
        "from_state": event.from_state.value if event.from_state else None,
        "to_state": event.to_state.value,
        "occurred_at": event.occurred_at,
        "reason": event.reason,
        "percentage": percentage,
    }
    data = json.dumps(payload, default=_json_default)
    return f"id: {event.sequence}\nevent: analysis.transition\ndata: {data}\n\n"


async def stream_analysis_events(
    db: Session,
    *,
    organization_id: uuid.UUID,
    analysis_id: uuid.UUID,
    since_sequence: int = 0,
    poll_interval: float = DEFAULT_POLL_INTERVAL_SECONDS,
    max_iterations: int | None = None,
) -> AsyncIterator[str]:
    """Yields SSE-formatted text for every `AnalysisEvent` with
    `sequence > since_sequence` (so a reconnecting client's `Last-Event-ID`
    resumes without replaying what it already saw), polling until the
    analysis reaches a stopping state (terminal or awaiting human review),
    then closes the stream. `max_iterations` exists only so tests don't have
    to wait out what would otherwise be an unbounded poll loop."""
    last_sequence = since_sequence
    iterations = 0
    while True:
        # A fresh statement in a fresh transaction sees every row any other
        # session (a worker process) has committed since the last poll -
        # under Postgres's default READ COMMITTED isolation, ending the
        # current transaction is what makes that guaranteed rather than
        # incidental.
        db.rollback()
        events = service.get_events(db, organization_id=organization_id, analysis_id=analysis_id)
        new_events = [e for e in events if e.sequence > last_sequence]
        analysis = service.get_analysis(
            db, organization_id=organization_id, analysis_id=analysis_id
        )
        for event in new_events:
            # The current (possibly-terminal) event uses the
            # `Analysis`-aware calculation (a `failed` outcome reports how
            # far it got, via `failure_stage`); every earlier event in this
            # batch was necessarily a live, non-stopping pipeline state.
            percentage = (
                progress_percentage(analysis)
                if event.sequence == events[-1].sequence
                else progress_percentage_for_state(event.to_state)
            )
            yield format_sse_event(event, percentage=percentage)
            last_sequence = event.sequence

        if analysis.state in STOPPING_STATES:
            return

        iterations += 1
        if max_iterations is not None and iterations >= max_iterations:
            return
        await asyncio.sleep(poll_interval)
