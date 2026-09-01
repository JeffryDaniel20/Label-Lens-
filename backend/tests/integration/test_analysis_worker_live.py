"""Real Arq + Redis verification for the queue infrastructure (P5-T2).

Runs actual Arq `Worker`s (in `burst=True` mode - process everything
currently queued, then stop) against a live Redis, proving the wiring
itself (job registration, `WorkerSettings`, cross-queue routing,
`on_startup`/`on_shutdown`) works, not just the pure Python logic already
covered by `tests/unit/test_analysis_worker.py`.

Marked `redis` and skipped without `LABELLENS_TEST_REDIS_URL`, the same
opt-in pattern as `postgres`/`object_storage`/`clamav`/`paddleocr`.
"""

from __future__ import annotations

import asyncio
import os

import pytest
from arq import Worker
from arq.connections import RedisSettings

from app.analysis import service
from app.analysis.models import AnalysisState
from app.analysis.worker import (
    QUEUE_DEFAULT,
    QUEUE_OCR,
    _on_shutdown,
    _on_startup,
    build_arq_pool,
    enqueue_first_stage,
    run_analysis_stage,
)
from app.catalog.models import File, FileStatus, Product, ProductVersion
from app.platform.config import get_settings
from tests.conftest import make_org

pytestmark = [pytest.mark.integration, pytest.mark.redis]

REDIS_URL = os.environ.get("LABELLENS_TEST_REDIS_URL")
pytestmark.append(
    pytest.mark.skipif(not REDIS_URL, reason="LABELLENS_TEST_REDIS_URL is not set")
)


def _make_analysis(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    db.add(
        File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key="k",
            original_filename="f.jpg",
            sha256="a" * 64,
            mime="image/jpeg",
            bytes=1,
            status=FileStatus.READY,
        )
    )
    db.flush()
    file_hash = service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)
    analysis, _ = service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return org, analysis


async def _run_burst(*, queue_name: str) -> None:
    worker = Worker(
        functions=[run_analysis_stage],
        queue_name=queue_name,
        redis_settings=RedisSettings.from_dsn(REDIS_URL),
        burst=True,
        max_jobs=10,
        on_startup=_on_startup,
        on_shutdown=_on_shutdown,
        # `handle_signals` is left at its default (True): `Worker.close()`
        # unconditionally calls `self.handle_sig(signal.SIGUSR1)` when this
        # is False, and `signal.SIGUSR1` does not exist on Windows. The
        # default path instead tries `loop.add_signal_handler(SIGINT/SIGTERM,
        # ...)`, which arq's own `_add_signal_handler` already wraps in a
        # try/except NotImplementedError for exactly this platform gap
        # (Windows' event loop doesn't support it) - so this is safe here.
    )
    await worker.async_run()
    await worker.close()


async def _enqueue_first(analysis_id: str, organization_id: str) -> None:
    pool = await build_arq_pool(get_settings())
    try:
        await enqueue_first_stage(pool, analysis_id=analysis_id, organization_id=organization_id)
    finally:
        await pool.close()


class TestRealArqWiring:
    def test_a_burst_worker_chains_through_every_default_queue_stage(self, db) -> None:
        org, analysis = _make_analysis(db)
        asyncio.run(_enqueue_first(str(analysis.id), str(org.id)))

        # One burst run on the default queue: queued -> validating ->
        # preprocessing -> ocr. The queue for the *next* job is chosen from
        # the state the analysis is *now* in (it determines which stage
        # function that job will run), so the job that runs while state is
        # already `preprocessing` (advancing it to `ocr`) is still a
        # default-queue job; only the job that will run *while* state is
        # `ocr` (advancing it to `extracting`) gets routed to the ocr queue
        # and is left unprocessed by this burst.
        asyncio.run(_run_burst(queue_name=QUEUE_DEFAULT))

        db.refresh(analysis)
        assert analysis.state is AnalysisState.OCR

        events = service.get_events(db, organization_id=org.id, analysis_id=analysis.id)
        assert [e.to_state for e in events] == [
            AnalysisState.QUEUED,
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.OCR,
        ]
        # Every transition was really performed by an Arq job, not this test.
        assert all(e.worker_id for e in events[1:])

    def test_the_next_stage_only_runs_once_a_worker_watches_its_queue(self, db) -> None:
        org, analysis = _make_analysis(db)
        asyncio.run(_enqueue_first(str(analysis.id), str(org.id)))

        asyncio.run(_run_burst(queue_name=QUEUE_DEFAULT))
        db.refresh(analysis)
        assert analysis.state is AnalysisState.OCR

        # A second default-queue burst finds nothing new (the pending job -
        # which will run the ocr stage function - is sitting on the *ocr*
        # queue, which this worker isn't watching).
        asyncio.run(_run_burst(queue_name=QUEUE_DEFAULT))
        db.refresh(analysis)
        assert analysis.state is AnalysisState.OCR

        # Only a worker actually watching the ocr queue picks it up.
        asyncio.run(_run_burst(queue_name=QUEUE_OCR))
        db.refresh(analysis)
        assert analysis.state is AnalysisState.EXTRACTING
