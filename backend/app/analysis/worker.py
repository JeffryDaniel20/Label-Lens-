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
than being killed mid-transaction. Real per-stage timeouts, exception-class-
specific retry policy, and a dead-letter queue are P5-T3's job, layered on
top of this.

Queue names follow IMPLEMENTATION.md §16: `default`, `ocr` (CPU-heavy, low
concurrency), `llm` (I/O-bound, higher concurrency) - `QUEUE_FOR_STAGE`
picks which one a given stage's job belongs on, so a worker process can be
scaled/configured per queue once the real OCR/LLM stage bodies exist.
"""

from __future__ import annotations

import os
import uuid
from typing import Any

from arq.connections import ArqRedis, RedisSettings, create_pool

from app.analysis import service
from app.analysis.models import AnalysisState
from app.analysis.stages import STOPPING_STATES, advance_analysis
from app.db.session import get_session_factory, init_engine, set_tenant_context
from app.platform.config import Settings, get_settings

QUEUE_DEFAULT = "default"
QUEUE_OCR = "ocr"
QUEUE_LLM = "llm"

QUEUE_FOR_STAGE: dict[AnalysisState, str] = {
    AnalysisState.OCR: QUEUE_OCR,
    AnalysisState.EXTRACTING: QUEUE_LLM,
}


def queue_for_state(state: AnalysisState) -> str:
    return QUEUE_FOR_STAGE.get(state, QUEUE_DEFAULT)


async def run_analysis_stage(ctx: dict[str, Any], analysis_id: str, organization_id: str) -> str:
    """The one Arq task this worker runs: advance `analysis_id` by exactly
    one stage, commit, and chain to the next stage's job unless the
    analysis has stopped. Returns the resulting state's value."""
    session_factory = get_session_factory()
    db = session_factory()
    try:
        org_uuid = uuid.UUID(organization_id)
        set_tenant_context(db, org_uuid)  # RLS context (no-op on SQLite)
        analysis = service.get_analysis(
            db, organization_id=org_uuid, analysis_id=uuid.UUID(analysis_id)
        )
        new_state = advance_analysis(db, analysis, worker_id=str(ctx.get("job_id", "")))
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
    analysis. Not yet called from `app.analysis.router` - submission and
    worker dispatch are wired together once this queue has a real stage to
    hand work to; see `app.analysis.stages`'s module docstring."""
    await pool.enqueue_job(
        "run_analysis_stage", analysis_id, organization_id, _queue_name=QUEUE_DEFAULT
    )


async def _on_startup(ctx: dict[str, Any]) -> None:
    settings = get_settings()
    init_engine(settings.database_url)


async def _on_shutdown(ctx: dict[str, Any]) -> None:
    return None


def build_redis_settings(settings: Settings) -> RedisSettings:
    return RedisSettings.from_dsn(settings.redis_url)


async def build_arq_pool(settings: Settings) -> ArqRedis:
    return await create_pool(build_redis_settings(settings))


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

    functions = (run_analysis_stage,)
    queue_name = QUEUE_DEFAULT
    redis_settings = _worker_redis_settings_from_env()
    on_startup = _on_startup
    on_shutdown = _on_shutdown
    max_jobs = 10
    max_tries = 5
    job_timeout = 300
    handle_signals = True
    job_completion_wait = 30
