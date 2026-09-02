"""Unit tests for per-stage cost/token recording (P5-T5)."""

from __future__ import annotations

import pytest

from app.analysis import costs, service
from app.catalog.models import File, FileStatus, Product, ProductVersion
from tests.conftest import make_org

pytestmark = pytest.mark.unit


@pytest.fixture
def analysis(db):
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
    row, _ = service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return row


class TestRecordStageCost:
    def test_a_fresh_analysis_starts_at_zero(self, analysis) -> None:
        assert analysis.total_tokens_in == 0
        assert analysis.total_tokens_out == 0
        assert analysis.total_cost_cents == 0

    def test_a_single_recording_sets_the_totals(self, db, analysis) -> None:
        costs.record_stage_cost(
            db,
            analysis,
            stage="ocr",
            provider="google_vision",
            tokens_in=0,
            tokens_out=0,
            cost_cents=12,
        )
        assert analysis.total_cost_cents == 12
        assert analysis.total_tokens_in == 0
        assert analysis.total_tokens_out == 0

    def test_repeated_recordings_across_stages_sum(self, db, analysis) -> None:
        costs.record_stage_cost(
            db, analysis, stage="ocr", provider="google_vision", cost_cents=12
        )
        costs.record_stage_cost(
            db,
            analysis,
            stage="extracting",
            provider="anthropic",
            tokens_in=1500,
            tokens_out=400,
            cost_cents=87,
        )
        costs.record_stage_cost(
            db,
            analysis,
            stage="extracting",
            provider="anthropic",
            tokens_in=300,
            tokens_out=50,
            cost_cents=9,
        )

        assert analysis.total_tokens_in == 1800
        assert analysis.total_tokens_out == 450
        assert analysis.total_cost_cents == 108

    def test_matches_provider_reported_usage_from_a_stub(self, db, analysis) -> None:
        # A stand-in for what a real provider client would report per call -
        # proving the sums genuinely match what was "reported," not just
        # that the function runs.
        stub_calls = [
            {"stage": "ocr", "provider": "google_vision", "cost_cents": 5},
            {
                "stage": "extracting",
                "provider": "anthropic",
                "tokens_in": 2000,
                "tokens_out": 600,
                "cost_cents": 120,
            },
        ]
        for call in stub_calls:
            costs.record_stage_cost(db, analysis, **call)

        assert analysis.total_tokens_in == sum(c.get("tokens_in", 0) for c in stub_calls)
        assert analysis.total_tokens_out == sum(c.get("tokens_out", 0) for c in stub_calls)
        assert analysis.total_cost_cents == sum(c["cost_cents"] for c in stub_calls)

    def test_recording_persists_across_a_refresh(self, db, analysis) -> None:
        costs.record_stage_cost(db, analysis, stage="ocr", provider="google_vision", cost_cents=42)
        db.commit()
        db.refresh(analysis)
        assert analysis.total_cost_cents == 42
