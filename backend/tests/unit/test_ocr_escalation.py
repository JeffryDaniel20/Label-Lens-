"""P3-T3: confidence-triggered OCR escalation.

Uses fake `OcrEngine`s so the policy itself is proven independently of any
specific vendor's behaviour: the primary engine always runs; the fallback
runs only when genuinely triggered; both attempts are recorded when it does;
the higher-confidence result is the one selected; and an exhausted budget
degrades gracefully rather than failing the analysis - the task's own
literal acceptance lines. The real Google Cloud Vision adapter (D-03,
resolved 2026-09-14) has its own contract tests in
`test_google_vision_ocr.py` and a live opt-in test in
`tests/integration/test_ocr_google_vision_live.py`; this module never makes
a real cloud call.
"""

from __future__ import annotations

import datetime as dt
import io
import uuid

import pytest
from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.db.base import utcnow
from app.platform.config import get_settings
from app.vision.models import OcrResult
from app.vision.ocr import build_ocr_fallback_engine
from app.vision.ocr.base import OcrToken
from app.vision.ocr.escalation import (
    EscalationPolicy,
    daily_fallback_calls_used,
    run_ocr_with_escalation,
)
from tests.conftest import make_org

pytestmark = pytest.mark.unit


class _FakeEngine:
    def __init__(self, name: str, confidence: float) -> None:
        self.name = name
        self.version = "1.0.0"
        self._confidence = confidence
        self.calls = 0

    def run(self, image):  # noqa: ANN001 - matches OcrEngine's own untyped-ndarray convention
        self.calls += 1
        return [
            OcrToken(
                text="NET QUANTITY 250 g",
                confidence=self._confidence,
                bbox=((0.0, 0.0), (10.0, 10.0)),
                line_no=1,
                language="en",
            )
        ]


class _FakeStorageClient:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self._objects = objects

    def download_object(self, key: str) -> bytes:
        return self._objects[key]


def _png_bytes() -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (100, 50), color=(255, 255, 255)).save(buf, format="PNG")
    return buf.getvalue()


@pytest.fixture
def org(db: Session):
    return make_org(db)


@pytest.fixture
def file_page(db: Session, org):
    product = Product(organization_id=org.id, name="P", internal_sku=f"S-{uuid.uuid4().hex[:6]}")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file_row = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="org/x/pv/y/z.png",
        original_filename="label.png",
        sha256="a" * 64,
        mime="image/png",
        bytes=123,
        status=FileStatus.READY,
    )
    db.add(file_row)
    db.flush()
    page = FilePage(
        organization_id=org.id,
        file_id=file_row.id,
        page_no=1,
        width=100,
        height=50,
        render_key="org/x/pv/y/render/sha/0001.png",
    )
    db.add(page)
    db.flush()
    return page


@pytest.fixture
def storage(file_page):
    return _FakeStorageClient({file_page.render_key: _png_bytes()})


DEFAULT_POLICY = EscalationPolicy(confidence_threshold=0.70, daily_budget_per_org=50)


class TestNoEscalationNeeded:
    def test_a_confident_primary_read_never_calls_the_fallback(
        self, db: Session, org, file_page, storage
    ) -> None:
        primary = _FakeEngine("primary", confidence=0.95)
        fallback = _FakeEngine("fallback", confidence=0.99)

        result = run_ocr_with_escalation(
            db,
            storage,
            organization_id=org.id,
            file_page=file_page,
            primary_engine=primary,
            fallback_engine=fallback,
            policy=DEFAULT_POLICY,
        )

        assert result.engine == "primary"
        assert result.selected is True
        assert fallback.calls == 0
        assert db.scalar(select(OcrResult).where(OcrResult.file_page_id == file_page.id).limit(2))


class TestEscalationTriggersAndRecordsBoth:
    def test_a_blurry_primary_read_escalates_once_and_records_both_results(
        self, db: Session, org, file_page, storage
    ) -> None:
        primary = _FakeEngine("primary", confidence=0.40)
        fallback = _FakeEngine("fallback", confidence=0.92)

        result = run_ocr_with_escalation(
            db,
            storage,
            organization_id=org.id,
            file_page=file_page,
            primary_engine=primary,
            fallback_engine=fallback,
            policy=DEFAULT_POLICY,
        )

        assert fallback.calls == 1
        assert result.engine == "fallback"
        assert result.selected is True

        rows = db.scalars(select(OcrResult).where(OcrResult.file_page_id == file_page.id)).all()
        assert {r.engine for r in rows} == {"primary", "fallback"}
        selected_engines = {r.engine for r in rows if r.selected}
        assert selected_engines == {"fallback"}

    def test_the_fallback_only_wins_if_it_actually_reads_better(
        self, db: Session, org, file_page, storage
    ) -> None:
        """Escalating is triggered by low primary confidence, but the
        fallback isn't blindly trusted - a still-worse fallback read leaves
        the primary selected."""
        primary = _FakeEngine("primary", confidence=0.40)
        fallback = _FakeEngine("fallback", confidence=0.35)

        result = run_ocr_with_escalation(
            db,
            storage,
            organization_id=org.id,
            file_page=file_page,
            primary_engine=primary,
            fallback_engine=fallback,
            policy=DEFAULT_POLICY,
        )

        assert fallback.calls == 1  # it still ran and was recorded
        assert result.engine == "primary"
        rows = db.scalars(select(OcrResult).where(OcrResult.file_page_id == file_page.id)).all()
        assert len(rows) == 2
        selected = [r for r in rows if r.selected]
        assert [r.engine for r in selected] == ["primary"]


class TestNoFallbackConfigured:
    def test_none_fallback_engine_is_a_graceful_no_op(
        self, db: Session, org, file_page, storage
    ) -> None:
        primary = _FakeEngine("primary", confidence=0.10)

        result = run_ocr_with_escalation(
            db,
            storage,
            organization_id=org.id,
            file_page=file_page,
            primary_engine=primary,
            fallback_engine=None,
            policy=DEFAULT_POLICY,
        )

        assert result.engine == "primary"
        assert result.selected is True
        rows = db.scalars(select(OcrResult).where(OcrResult.file_page_id == file_page.id)).all()
        assert len(rows) == 1


class TestBudgetExhaustionDegradesGracefully:
    def test_an_exhausted_daily_budget_skips_escalation_without_failing(
        self, db: Session, org, file_page, storage
    ) -> None:
        # Pre-populate today's budget with fallback-engine results from
        # other pages, as if this org already escalated a lot today.
        for _ in range(3):
            db.add(
                OcrResult(
                    organization_id=org.id,
                    file_page_id=file_page.id,
                    engine="fallback",
                    engine_version="1",
                    avg_confidence=0.9,
                    raw=[],
                )
            )
        db.commit()

        tight_policy = EscalationPolicy(confidence_threshold=0.70, daily_budget_per_org=3)
        primary = _FakeEngine("primary", confidence=0.10)
        fallback = _FakeEngine("fallback", confidence=0.99)

        result = run_ocr_with_escalation(
            db,
            storage,
            organization_id=org.id,
            file_page=file_page,
            primary_engine=primary,
            fallback_engine=fallback,
            policy=tight_policy,
        )

        assert fallback.calls == 0, "budget already exhausted - never call the fallback"
        assert result.engine == "primary"
        assert result.selected is True

    def test_daily_fallback_calls_used_counts_only_non_primary_recent_results(
        self, db: Session, org, file_page
    ) -> None:
        now = utcnow()
        db.add(
            OcrResult(
                organization_id=org.id,
                file_page_id=file_page.id,
                engine="primary",
                engine_version="1",
                avg_confidence=0.9,
                raw=[],
            )
        )
        db.add(
            OcrResult(
                organization_id=org.id,
                file_page_id=file_page.id,
                engine="fallback",
                engine_version="1",
                avg_confidence=0.9,
                raw=[],
            )
        )
        db.commit()

        used = daily_fallback_calls_used(
            db, organization_id=org.id, primary_engine_name="primary", now=now
        )
        assert used == 1

        stale = daily_fallback_calls_used(
            db,
            organization_id=org.id,
            primary_engine_name="primary",
            now=now + dt.timedelta(days=2),
        )
        assert stale == 0, "a fallback call from more than a day ago no longer counts"


class TestFallbackProviderSelection:
    def test_the_default_null_provider_disables_escalation(self) -> None:
        get_settings.cache_clear()
        settings = get_settings()
        assert settings.ocr_fallback_provider == "null"
        assert build_ocr_fallback_engine(settings) is None

    def test_an_unknown_provider_string_raises_rather_than_silently_disabling(self) -> None:
        settings = get_settings().model_copy(update={"ocr_fallback_provider": "not-a-real-vendor"})
        with pytest.raises(ValueError, match="Unknown OCR fallback provider"):
            build_ocr_fallback_engine(settings)

    def test_google_vision_selected_without_a_credential_degrades_to_disabled(self) -> None:
        """An operator flipping the provider on before the key is set gets
        escalation quietly staying off, not a pipeline-wide crash on the
        next analysis - unlike the LLM provider, OCR escalation is optional
        by design, so there is always a safe fallback: the primary result."""
        settings = get_settings().model_copy(
            update={
                "ocr_fallback_provider": "google_vision",
                "ocr_fallback_google_vision_api_key": "",
            }
        )
        assert build_ocr_fallback_engine(settings) is None

    def test_google_vision_selected_with_a_credential_builds_the_real_engine(self) -> None:
        settings = get_settings().model_copy(
            update={
                "ocr_fallback_provider": "google_vision",
                "ocr_fallback_google_vision_api_key": "test-key-not-a-real-credential",
            }
        )
        engine = build_ocr_fallback_engine(settings)
        assert engine is not None
        assert engine.name == "google_vision"
