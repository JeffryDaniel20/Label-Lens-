"""Confidence-triggered OCR escalation (P3-T3).

IMPLEMENTATION.md names a concrete cloud vendor (Google Cloud Vision) for
this fallback, but **D-03 (which vendor - Google Vision vs Azure Read) is
still an open decision**, and whichever is chosen additionally needs real
cloud credentials this environment does not have - the same honest blocker
`app.extraction.llm` records for D-06 until it was resolved, and the same
one P3-T3's own row in TESTTEST.md has recorded since P3-T2.

What *is* real and shipped here is the vendor-neutral escalation policy
itself, mirroring `app/extraction/llm/base.py::ExtractionProvider`'s own
"a vendor sits behind a narrow Protocol" posture: `run_ocr_with_escalation`
runs the primary engine, and escalates to a second `OcrEngine` - any
`OcrEngine`, real or fake, cloud or not - only when three independent
conditions all hold: the primary result's own confidence is below
threshold, a fallback engine is actually configured
(`app.vision.ocr.build_ocr_fallback_engine` returns `None` today, since
`LABELLENS_OCR_FALLBACK_PROVIDER` has no real option yet), and the
organization's own daily fallback budget isn't already exhausted. Budget
exhaustion (the task's own literal acceptance line, "degrades gracefully")
means silently keeping the primary result - never failing the analysis over
a missing or exhausted fallback.

Both attempts are recorded as their own real `OcrResult` row whenever
escalation actually runs (the task's other literal acceptance line) - nothing
here discards the primary read even when the fallback turns out to win.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.catalog.models import FilePage
from app.db.base import utcnow
from app.storage.client import ObjectStorageClient
from app.vision.models import OcrResult
from app.vision.ocr.base import OcrEngine
from app.vision.ocr.service import run_ocr


@dataclass(slots=True, frozen=True)
class EscalationPolicy:
    confidence_threshold: float
    daily_budget_per_org: int


def daily_fallback_calls_used(
    db: Session,
    *,
    organization_id: uuid.UUID,
    primary_engine_name: str,
    now: dt.datetime | None = None,
) -> int:
    """How many fallback-engine `OcrResult`s this org has produced in the
    last 24 hours - a live count over real rows, not a separately
    maintained running total that could drift from the truth (the same
    posture `app.platform.metrics`'s own gauges use, P7-T5)."""
    now = now or utcnow()
    since = now - dt.timedelta(days=1)
    count = db.scalar(
        select(func.count())
        .select_from(OcrResult)
        .where(
            OcrResult.organization_id == organization_id,
            OcrResult.engine != primary_engine_name,
            OcrResult.created_at >= since,
        )
    )
    return count or 0


def run_ocr_with_escalation(
    db: Session,
    storage: ObjectStorageClient,
    *,
    organization_id: uuid.UUID,
    file_page: FilePage,
    primary_engine: OcrEngine,
    fallback_engine: OcrEngine | None,
    policy: EscalationPolicy,
) -> OcrResult:
    """Always runs `primary_engine`. Escalates to `fallback_engine` only
    when its own confidence is below `policy.confidence_threshold`, a
    fallback engine is actually configured, and the org's daily budget
    isn't exhausted. When both engines run, whichever produced the higher
    `avg_confidence` is marked `selected=True` and returned - the other stays
    recorded but unselected, so `app.extraction.service.load_ocr_tokens`
    reads exactly one page's worth of tokens either way."""
    primary_result = run_ocr(
        db, storage, primary_engine, organization_id=organization_id, file_page=file_page
    )
    if fallback_engine is None:
        return primary_result
    if primary_result.avg_confidence >= policy.confidence_threshold:
        return primary_result

    used = daily_fallback_calls_used(
        db, organization_id=organization_id, primary_engine_name=primary_engine.name
    )
    if used >= policy.daily_budget_per_org:
        return primary_result

    fallback_result = run_ocr(
        db, storage, fallback_engine, organization_id=organization_id, file_page=file_page
    )
    if fallback_result.avg_confidence <= primary_result.avg_confidence:
        # The fallback ran (and is recorded) but didn't actually read
        # better - `run_ocr` gives every new `OcrResult` `selected=True` by
        # default, so this one must be explicitly demoted or the page would
        # end up with two "selected" results at once.
        fallback_result.selected = False
        db.add(fallback_result)
        db.flush()
        return primary_result

    primary_result.selected = False
    fallback_result.selected = True
    db.add(primary_result)
    db.add(fallback_result)
    db.flush()
    return fallback_result
