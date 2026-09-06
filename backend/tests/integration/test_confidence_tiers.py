"""Integration tests for P3-T8's DB-touching half: `compute_analysis_tier`
aggregating real `ExtractedField`/`OcrTokenRow` rows, and the `_scoring`
stage function that persists the result onto `Analysis.confidence_tier` and
routes to `needs_review` or lets `completed` fall through. The pure
confidence math has its own unit suite in
`tests/unit/test_confidence_tiers.py`.
"""

from __future__ import annotations

import uuid

import pytest

from app.analysis.models import Analysis, AnalysisState, ConfidenceTier
from app.analysis.retry_policy import PermanentStageError
from app.analysis.stages import _scoring
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.confidence.tiers import compute_analysis_tier
from app.extraction.models import ExtractedField, Extraction
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

pytestmark = pytest.mark.integration


@pytest.fixture
def rig(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="k",
        original_filename="f.jpg",
        sha256="a" * 64,
        mime="image/jpeg",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=10, height=10, render_key="r"
    )
    db.add(page)
    db.flush()
    analysis = Analysis(
        organization_id=org.id,
        product_version_id=version.id,
        state=AnalysisState.SCORING,
        idempotency_key=uuid.uuid4().hex,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org.id,
        analysis_id=analysis.id,
        schema_version="1.0.0",
        payload={},
        envelope={},
        provider="stub",
        model="stub",
        prompt_version="1",
        prompt_hash="h",
    )
    db.add(extraction)
    db.flush()
    return org, page, analysis, extraction


def _token(db, org, page, *, confidence: float) -> str:
    result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="stub",
        engine_version="1",
        avg_confidence=confidence,
        raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org.id,
        file_page_id=page.id,
        ocr_result_id=result.id,
        text="x",
        confidence=confidence,
        x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        line_no=0,
    )
    db.add(token)
    db.flush()
    return str(token.id)


def _field(
    db, org, extraction, *, field_path: str, value_raw: str | None,
    confidence: float = 0.9, verified: bool | None = None,
    cited_token_ids: list[str] | None = None,
) -> ExtractedField:
    field = ExtractedField(
        organization_id=org.id,
        extraction_id=extraction.id,
        field_path=field_path,
        value_raw=value_raw,
        confidence=confidence,
        verified=verified,
        cited_token_ids=cited_token_ids or [],
    )
    db.add(field)
    db.flush()
    return field


class TestComputeAnalysisTier:
    def test_all_fields_verified_and_confident_yields_high(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[tok],
        )

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.tier is ConfidenceTier.HIGH
        assert result.fields[0].confidence == pytest.approx(0.95)

    def test_a_medium_band_field_caps_the_whole_analysis_at_medium(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        high_tok = _token(db, org, page, confidence=0.95)
        medium_tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[high_tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.8, verified=True, cited_token_ids=[medium_tok],
        )

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.tier is ConfidenceTier.MEDIUM

    def test_a_missing_field_forces_low_even_with_other_high_confidence_fields(
        self, db, rig
    ) -> None:
        """The acceptance criterion's own test line: a missing rule-relevant
        field forces Low."""
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw=None,
            confidence=0.0, verified=None,
        )

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.tier is ConfidenceTier.LOW
        missing = next(f for f in result.fields if f.field_path == "dates.batch_number")
        assert missing.reason == "field was not found on the label"

    def test_a_demoted_field_forces_low(self, db, rig) -> None:
        """A field P3-T6 demoted (hallucination/uncited) must never be
        rescued by an otherwise-high extraction confidence."""
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.99, verified=False,
        )

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.tier is ConfidenceTier.LOW
        demoted = next(f for f in result.fields if f.field_path == "dates.batch_number")
        assert demoted.reason == "field failed evidence verification"

    def test_field_confidence_uses_the_worst_cited_token_not_the_average(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        strong = _token(db, org, page, confidence=0.99)
        weak = _token(db, org, page, confidence=0.5)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[strong, weak],
        )

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.fields[0].confidence == pytest.approx(0.5)
        assert result.tier is ConfidenceTier.LOW

    def test_no_extracted_fields_at_all_is_low(self, db, rig) -> None:
        _org, _page, _analysis, extraction = rig

        result = compute_analysis_tier(db, extraction=extraction)

        assert result.tier is ConfidenceTier.LOW
        assert result.fields[0].reason == "no fields were extracted"


class TestScoringStage:
    def test_a_high_tier_extraction_falls_through_to_the_default_successor(
        self, db, rig
    ) -> None:
        org, page, analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[tok],
        )

        next_state = _scoring(db, analysis)
        db.commit()

        assert next_state is None
        db.refresh(analysis)
        assert analysis.confidence_tier is ConfidenceTier.HIGH

    def test_a_low_tier_extraction_routes_explicitly_to_needs_review(self, db, rig) -> None:
        org, page, analysis, extraction = rig
        _field(db, org, extraction, field_path="quantity.net_quantity", value_raw=None)

        next_state = _scoring(db, analysis)
        db.commit()

        assert next_state is AnalysisState.NEEDS_REVIEW
        db.refresh(analysis)
        assert analysis.confidence_tier is ConfidenceTier.LOW

    def test_no_extraction_is_a_permanent_failure(self, db, rig) -> None:
        """A fresh analysis that never reached `extracting` at all - not the
        fixture's own analysis, which already has an `Extraction` row."""
        _org, _page, analysis, _extraction = rig
        bare = Analysis(
            organization_id=analysis.organization_id,
            product_version_id=analysis.product_version_id,
            state=AnalysisState.SCORING,
            idempotency_key=uuid.uuid4().hex,
        )
        db.add(bare)
        db.flush()

        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _scoring(db, bare)
