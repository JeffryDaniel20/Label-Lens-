"""Arq worker: queue setup, the chained stage job, and graceful shutdown
(P5-T2).

One job (`run_analysis_stage`) advances one analysis by exactly one stage
(`app.analysis.stages.advance_analysis`) and, unless the analysis has
stopped (terminal or awaiting human review), re-enqueues itself for the
next stage - the "chaining" this task calls for. Each stage's own
transition commits independently the moment it succeeds (see
`app.analysis.stages`'s module docstring for why that alone is the
checkpoint this queue needs), so a job that's retried after a worker crash
picks up exactly where the analysis's own persisted state says it left off.

**Retryable and observable** (this task's acceptance criterion): a stage
function's exception propagates out of `run_analysis_stage` without
committing anything, which is exactly what makes Arq's own per-job retry
(`max_tries`) safe to rely on here - a retried job re-attempts only the one
stage that failed, never a stage that already committed. Every transition,
successful or not yet reached, is recorded as an `AnalysisEvent` carrying
the Arq job id as `worker_id`, so which worker attempt produced which state
change is always reconstructable after the fact.

**Graceful shutdown**: Arq's own signal handling (`handle_signals=True`,
the default) and `job_completion_wait` give an in-flight job time to finish
- and commit - before the process actually exits on SIGTERM/SIGINT, rather
than being killed mid-transaction.

**Retries, timeouts, DLQ, and the janitor (P5-T3)** are layered on top of
the P5-T2 mechanism above without changing it: `run_analysis_stage` itself
now classifies a stage function's exception (`app.analysis.retry_policy`),
enforces the whole-analysis time budget before attempting the next stage,
and records a `DeadLetterJob` (`app.analysis.dlq`) whenever it gives up
automatically retrying. `WorkerSettings.cron_jobs` schedules
`app.analysis.janitor.reap_stalled_analyses_job` to catch the case this
worker-level logic structurally cannot: an analysis whose *next* job never
arrives at all (lost from the queue, not just a job that failed).

Queue names follow IMPLEMENTATION.md §16: `default`, `ocr` (CPU-heavy, low
concurrency), `llm` (I/O-bound, higher concurrency) - `QUEUE_FOR_STAGE`
picks which one a given stage's job belongs on. Both the OCR (P3-T2/this
vertical-slice wiring) and LLM (P3-T5) stage bodies are real now, so a
production deployment genuinely runs separate worker processes per queue,
sized independently - a CPU-bound PaddleOCR pool and an I/O-bound Gemini
pool have very different concurrency sweet spots. `LABELLENS_WORKER_QUEUE`
(`_worker_queue_name_from_env`, below) is what actually makes that
startable via the standard `arq app.analysis.worker.WorkerSettings` CLI
entrypoint alone: run it three times with `LABELLENS_WORKER_QUEUE` set to
`default`/`ocr`/`llm` respectively (see `infra/docker-compose.yml`'s
`worker-default`/`worker-ocr`/`worker-llm` services) rather than needing a
bespoke script per queue.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from arq import cron
from arq.connections import ArqRedis, RedisSettings, create_pool
from arq.worker import Retry
from sqlalchemy.orm import Session

from app.analysis import retry_policy, service
from app.analysis.dlq import record_dead_letter
from app.analysis.janitor import reap_stalled_analyses_job
from app.analysis.models import Analysis, AnalysisState, DeadLetterReason
from app.analysis.retry_policy import PermanentStageError, TransientStageError
from app.analysis.stages import STOPPING_STATES, advance_analysis
from app.analysis.state_machine import transition
from app.db import models as _models  # noqa: F401 - registers every mapped table's metadata
from app.db.session import get_session_factory, init_engine, set_tenant_context
from app.platform.config import Settings, get_settings
from app.platform.metrics import STAGE_DURATION_SECONDS
from app.reports.worker import render_report_pdf_job

QUEUE_DEFAULT = "default"
QUEUE_OCR = "ocr"
QUEUE_LLM = "llm"

QUEUE_FOR_STAGE: dict[AnalysisState, str] = {
    AnalysisState.OCR: QUEUE_OCR,
    AnalysisState.EXTRACTING: QUEUE_LLM,
}


def queue_for_state(state: AnalysisState) -> str:
    return QUEUE_FOR_STAGE.get(state, QUEUE_DEFAULT)


def _fail_and_dead_letter(
    db: Session,
    analysis: Analysis,
    *,
    worker_id: str,
    reason: str,
    error_message: str,
    retryable: bool,
    attempt: int,
    dlq_reason: DeadLetterReason,
) -> AnalysisState:
    """Shared tail of every give-up path: transition to `failed`, record the
    dead letter, return the resulting state. `analysis.state` (the stage
    that was actually running) is captured *before* the transition."""
    failed_stage = analysis.state.value
    transition(
        db,
        analysis,
        AnalysisState.FAILED,
        worker_id=worker_id,
        reason=reason,
        failure_stage=failed_stage,
        retryable=retryable,
    )
    record_dead_letter(
        db,
        analysis=analysis,
        stage=failed_stage,
        reason=dlq_reason,
        error_message=error_message,
        attempt_count=attempt,
    )
    return analysis.state


async def run_analysis_stage(ctx: dict[str, Any], analysis_id: str, organization_id: str) -> str:
    """The one Arq task this worker runs: advance `analysis_id` by exactly
    one stage, commit, and chain to the next stage's job unless the
    analysis has stopped. Returns the resulting state's value.

    **Retry/DLQ policy (P5-T3):** a stage function may raise
    `TransientStageError` (retried up to `retry_policy.MAX_STAGE_ATTEMPTS`
    times with backoff+jitter, via Arq's own `Retry`, then dead-lettered as
    still-`retryable`) or `PermanentStageError` (dead-lettered immediately,
    never automatically retried). Anything else propagates unclassified, the
    same as P5-T2 - Arq's own `max_tries` still applies as the backstop.
    Separately, an analysis that has been running longer than
    `retry_policy.ANALYSIS_BUDGET_SECONDS` is force-failed as a timeout
    before its next stage is even attempted."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        org_uuid = uuid.UUID(organization_id)
        set_tenant_context(db, org_uuid)  # RLS context (no-op on SQLite)
        analysis = service.get_analysis(
            db, organization_id=org_uuid, analysis_id=uuid.UUID(analysis_id)
        )
        worker_id = str(ctx.get("job_id", ""))

        if analysis.state not in STOPPING_STATES and retry_policy.is_budget_exceeded(
            analysis.started_at
        ):
            new_state = _fail_and_dead_letter(
                db,
                analysis,
                worker_id=worker_id,
                reason="Exceeded the whole-analysis time budget.",
                error_message=(
                    f"Analysis exceeded its {retry_policy.ANALYSIS_BUDGET_SECONDS}s "
                    "whole-pipeline budget."
                ),
                retryable=False,
                attempt=1,
                dlq_reason=DeadLetterReason.TIMEOUT,
            )
            db.commit()
            return new_state.value

        stage_being_run = analysis.state.value
        try:
            with STAGE_DURATION_SECONDS.labels(stage=stage_being_run).time():
                new_state = advance_analysis(db, analysis, worker_id=worker_id)
        except (TransientStageError, PermanentStageError) as exc:
            attempt = int(ctx.get("job_try", 1))
            if isinstance(exc, TransientStageError) and attempt < retry_policy.MAX_STAGE_ATTEMPTS:
                db.rollback()
                raise Retry(defer=retry_policy.backoff_seconds(attempt)) from exc
            new_state = _fail_and_dead_letter(
                db,
                analysis,
                worker_id=worker_id,
                reason=str(exc),
                error_message=str(exc),
                retryable=isinstance(exc, TransientStageError),
                attempt=attempt,
                dlq_reason=(
                    DeadLetterReason.STAGE_EXHAUSTED
                    if isinstance(exc, TransientStageError)
                    else DeadLetterReason.PERMANENT_ERROR
                ),
            )
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()

    if new_state not in STOPPING_STATES:
        pool: ArqRedis = ctx["redis"]
        await pool.enqueue_job(
            "run_analysis_stage",
            analysis_id,
            organization_id,
            _queue_name=queue_for_state(new_state),
        )
    return new_state.value


async def enqueue_first_stage(
    pool: ArqRedis, *, analysis_id: str, organization_id: str
) -> None:
    """Enqueue the very first stage job for a newly-created (`queued`)
    analysis. Called from `app.analysis.router.submit_analysis` via the
    app-lifecycle-managed pool in `app.state.arq_pool`."""
    await pool.enqueue_job(
        "run_analysis_stage", analysis_id, organization_id, _queue_name=QUEUE_DEFAULT
    )


async def _on_startup(ctx: dict[str, Any]) -> None:
    """Called once when this worker process starts.

    A standalone `arq app.analysis.worker.WorkerSettings` process never
    imports `app.main`, which is normally what pulls in `app.db.models` (the
    module that imports every mapped table so SQLAlchemy's registry can
    resolve cross-table foreign keys) - hence this module's own top-level
    `from app.db import models as _models` import. Found the hard way, live:
    without it, the very first real flush touching `analyses` raises
    `NoReferencedTableError` trying to resolve `ruleset_version_id`'s FK to
    `rulesets`, a table this process had genuinely never heard of.
    """
    settings = get_settings()
    init_engine(settings.database_url)


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    return None


def build_redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def build_arq_pool(settings: Settings) -> ArqRedis:
    return await create_pool(build_redis_settings(settings))


def _worker_queue_name_from_env() -> str:
    """`arq`'s CLI (`arq app.analysis.worker.WorkerSettings`) has no `--queue`
    flag - `queue_name` is read once from this class as a plain attribute,
    the same constraint documented on `redis_settings` below. Left at its
    default, every worker process started this way only ever watches
    `default`, so the moment any analysis reaches `ocr` or `extracting`
    (see `QUEUE_FOR_STAGE`) its next job sits enqueued forever with nothing
    to dequeue it - found live wiring up a real end-to-end run: the
    2026-09-04 manual verification worked around this by constructing
    `arq.Worker(..., queue_name=...)` directly in a throwaway script instead
    of the standard CLI entrypoint, which is not a repeatable deployment
    story. `LABELLENS_WORKER_QUEUE` makes the same one process/one queue
    real workers already need (per this module's own docstring - a
    CPU-bound PaddleOCR pool and an I/O-bound Gemini pool want very
    different concurrency) actually startable via `arq <module>.WorkerSettings`
    alone, no bespoke script required."""
    return os.environ.get("LABELLENS_WORKER_QUEUE", QUEUE_DEFAULT)


def _worker_redis_settings_from_env() -> RedisSettings:
    """`LABELLENS_REDIS_URL` may be `memory://` (the API's in-memory session
    fallback, used by default in tests) - not a real redis DSN, and not
    something `RedisSettings.from_dsn` can parse. A worker process cannot
    run without real Redis regardless, so this falls back to arq's own
    default (`localhost:6379`) rather than crashing at import time - real
    deployments always set a genuine `redis://` URL."""
    dsn = os.environ.get("LABELLENS_REDIS_URL", "redis://localhost:6379/0")
    try:
        return RedisSettings.from_dsn(dsn)
    except RuntimeError:
        return RedisSettings()


class WorkerSettings:
    """Arq's own entrypoint contract (`arq app.analysis.worker.WorkerSettings`).

    `redis_settings` is read from the raw `LABELLENS_REDIS_URL` environment
    variable directly, not via `get_settings()`/`Settings` - a dedicated
    worker process's Redis connection shouldn't depend on the full app
    config (secret key, database URL, etc.) validating successfully first,
    and `arq`'s own CLI (`get_kwargs()`) reads this as a plain class
    attribute, evaluated once at import time - it cannot be a method.
    """

    functions = (run_analysis_stage, render_report_pdf_job)
    # Every minute: cheap (an indexed state filter over `analyses`) and the
    # budget itself is measured in minutes, so sub-minute reaping precision
    # buys nothing.
    cron_jobs = (cron(reap_stalled_analyses_job, minute=set(range(60))),)
    queue_name = _worker_queue_name_from_env()
    redis_settings = _worker_redis_settings_from_env()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 10
    max_tries = 5
    job_timeout = 300
    handle_signals = True
    job_completion_wait = 30
