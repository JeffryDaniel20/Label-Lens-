"""Stalled-analysis reaper (P5-T3).

An analysis that hasn't reached a terminal or human-review state within its
whole-pipeline budget is presumed dead - most likely a worker crashed
without ever getting the chance to run `run_analysis_stage` again (the
crash-mid-stage case P5-T2 already checkpoints correctly *is* recoverable by
a retried job; this reaper is the backstop for the case where no job to
retry it ever arrives at all, e.g. it was lost from the queue). The janitor
periodically scans for these and force-fails them, so the acceptance
criterion "no analysis can remain non-terminal beyond its budget" holds even
when nothing else would ever revisit that row again.

Deliberately a **cross-organization** scan: reaping is a platform-maintenance
operation, not a tenant-scoped one, so it does not set `app.org_id` at all.
The RLS policies on `analyses` (migration 0006) explicitly allow this - see
`... OR coalesce(current_setting('app.org_id', true), '') = ''` in each
policy - the same "no tenant context set = maintenance access" convention
already used nowhere else yet, established here for the first time.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import retry_policy
from app.analysis.dlq import record_dead_letter
from app.analysis.models import Analysis, AnalysisState, DeadLetterReason
from app.analysis.state_machine import transition
from app.db.session import get_session_factory, set_tenant_context

# The states the automated pipeline drives on its own - the ones a stall
# actually applies to (`queued` plus every stage in `STAGE_SEQUENCE`).
# `needs_review`/`review` are excluded on purpose: a human reviewer taking
# longer than the pipeline's own budget is not a stall, it's just review
# taking a while. Terminal states are obviously excluded too.
_EXCLUDED_FROM_REAPING = frozenset(
    {
        AnalysisState.NEEDS_REVIEW,
        AnalysisState.REVIEW,
        AnalysisState.COMPLETED,
        AnalysisState.FAILED,
        AnalysisState.CANCELLED,
    }
)
REAPABLE_STATES: frozenset[AnalysisState] = frozenset(AnalysisState) - _EXCLUDED_FROM_REAPING


def reap_stalled_analyses(db: Session) -> list[Analysis]:
    """Finds every non-terminal, non-review analysis whose `started_at` is
    older than the whole-pipeline budget, force-fails each one
    (`failed(stalled)`, `retryable=True` - a stall is presumed transient,
    e.g. a crashed worker, not a bad input), records a dead letter for it,
    and returns the list of analyses it reaped."""
    set_tenant_context(db, None)  # cross-org: see the RLS-policy note above
    candidates = db.scalars(
        select(Analysis).where(Analysis.state.in_(REAPABLE_STATES))
    ).all()

    reaped: list[Analysis] = []
    for analysis in candidates:
        if not retry_policy.is_budget_exceeded(analysis.started_at):
            continue
        stalled_stage = analysis.state.value
        transition(
            db,
            analysis,
            AnalysisState.FAILED,
            reason="Reaped by the janitor: no progress within the whole-analysis budget.",
            failure_stage=stalled_stage,
            retryable=True,
        )
        record_dead_letter(
            db,
            analysis=analysis,
            stage=stalled_stage,
            reason=DeadLetterReason.STALLED,
            error_message=(
                f"Analysis stalled in {stalled_stage!r} beyond the "
                f"{retry_policy.ANALYSIS_BUDGET_SECONDS}s budget."
            ),
            attempt_count=1,
        )
        reaped.append(analysis)
    db.commit()
    return reaped


async def reap_stalled_analyses_job(ctx: dict[str, Any]) -> int:
    """The Arq cron entry point - a periodic maintenance job, not a
    per-analysis stage job. Opens its own session against the engine
    `app.analysis.worker._on_startup` already initialised for this worker
    process (the same one `run_analysis_stage` uses) - a cron job does not
    get its own separate startup hook."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        reaped = reap_stalled_analyses(db)
        return len(reaped)
    finally:
        db.close()
