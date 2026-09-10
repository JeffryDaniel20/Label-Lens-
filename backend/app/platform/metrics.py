"""Prometheus metrics (P7-T5).

IMPLEMENTATION.md section 20's own list drives what's instrumented here:
"analyses by state, stage durations (p50/p95), ... queue depth, DLQ size" -
this module defines the metric objects; the actual `.inc()`/`.observe()`
calls live at the point each event genuinely happens
(`app.analysis.state_machine.transition`, `app.analysis.dlq.record_dead_letter`,
`app.analysis.worker.run_analysis_stage`), not scattered ad hoc across the
codebase. `render_metrics()` is the one function `GET /metrics` calls.

Counters/histograms accumulate in-process (correct for a single API/worker
instance; a multi-process deployment needs Prometheus's own multiprocess
mode, out of scope for this task's local-VPS deployment target per
IMPLEMENTATION.md section 4). Gauges that represent *current* state
(`DLQ_UNREPLAYED`) are deliberately set from a live DB query at scrape time
in `app.main`'s `/metrics` handler rather than incremented/decremented at
every write site - a query can never drift from the truth the way a
hand-maintained running total could (a missed decrement on replay would
leave the gauge permanently wrong).
"""

from __future__ import annotations

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)

REGISTRY = CollectorRegistry()

ANALYSES_TRANSITIONS_TOTAL = Counter(
    "labellens_analysis_transitions_total",
    "Analysis state transitions, by the state reached.",
    ["state"],
    registry=REGISTRY,
)

ANALYSIS_FAILURES_TOTAL = Counter(
    "labellens_analysis_failures_total",
    "Analyses that reached the failed state, by the stage that was running.",
    ["stage"],
    registry=REGISTRY,
)

STAGE_DURATION_SECONDS = Histogram(
    "labellens_stage_duration_seconds",
    "Wall-clock time for one pipeline stage's own function to run.",
    ["stage"],
    registry=REGISTRY,
)

DLQ_ARRIVALS_TOTAL = Counter(
    "labellens_dlq_arrivals_total",
    "Dead-letter jobs recorded, by reason.",
    ["reason"],
    registry=REGISTRY,
)

# Set at scrape time from a live query - see the module docstring for why
# this is a Gauge assigned in app.main rather than inc/dec'd at write sites.
DLQ_UNREPLAYED = Gauge(
    "labellens_dlq_unreplayed",
    "Currently unreplayed dead-letter jobs, across all organizations.",
    registry=REGISTRY,
)

ANALYSES_BY_STATE = Gauge(
    "labellens_analyses_by_state",
    "Analyses currently in each state, across all organizations.",
    ["state"],
    registry=REGISTRY,
)

QUEUE_DEPTH = Gauge(
    "labellens_queue_depth",
    "Pending Arq jobs per queue, read live from Redis at scrape time.",
    ["queue"],
    registry=REGISTRY,
)


def render_metrics() -> tuple[bytes, str]:
    """Returns (body, content_type) for `GET /metrics` to hand straight to
    a `Response`."""
    return generate_latest(REGISTRY), CONTENT_TYPE_LATEST
