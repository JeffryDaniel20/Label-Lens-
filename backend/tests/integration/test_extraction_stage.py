"""The `extracting` pipeline stage (P3-T5 wired into P5-T2/P5-T3).

Covers the seam between extraction and the queue: that a provider/credential
problem is classified into P5-T3's retry vocabulary correctly, and that real
provider spend lands on the analysis via P5-T5's cost accounting.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis import service as analysis_service
from app.analysis.models import AnalysisState
from app.analysis.retry_policy import PermanentStageError, TransientStageError
from app.analysis.stages import STAGE_FUNCTIONS, advance_analysis
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction import facts as facts_schema
from app.extraction.llm import ProviderError, ProviderNotConfigured
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.platform.config import Settings
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org
from tests.integration.test_extraction_service import VALID_JSON

pytestmark = pytest.mark.integration


class _StubProvider:
    name = "stub"

    def __init__(self, text: str = VALID_JSON) -> None:
        self.text = text

    def generate(self, *, system_instruction, prompt, schema, model) -> ProviderResponse:
        return ProviderResponse(
            text=self.text, usage=ProviderUsage(tokens_in=120, tokens_out=80), model=model
        )


class _RaisingProvider:
    name = "stub"

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def generate(self, *, system_instruction, prompt, schema, model) -> ProviderResponse:
        raise self.exc


@pytest.fixture
def analysis_at_extracting(db, monkeypatch):
    """An analysis already advanced to the `extracting` state, with tokens."""
    org = make_org(db)
    product = Product(organization_id=org.id, name="Chips", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="k",
        original_filename="label.jpg",
        sha256="c" * 64,
        mime="image/jpeg",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=800, height=600, render_key="r"
    )
    db.add(page)
    db.flush()
    result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="stub",
        engine_version="1",
        avg_confidence=0.9,
        raw=[],
    )
    db.add(result)
    db.flush()
    for i, text in enumerate(("Wheat flour", "250 g")):
        db.add(
            OcrTokenRow(
                organization_id=org.id,
                file_page_id=page.id,
                ocr_result_id=result.id,
                text=text,
                confidence=0.9,
                x1=float(i),
                y1=0.0,
                x2=float(i + 1),
                y2=1.0,
                line_no=i,
            )
        )
    db.flush()
    file_hash = analysis_service.compute_file_set_hash(
        db, organization_id=org.id, version_id=version.id
    )
    analysis, _ = analysis_service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    # The fixture already built a real `OcrResult`/tokens by hand above -
    # `_ocr` running for real would just redundantly (and, without a live
    # PaddleOCR engine, unsuccessfully) try to reproduce that, so it's
    # stubbed to a no-op for this walk only; this file's own tests are about
    # the `extracting` stage, not `ocr`'s.
    monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.OCR, lambda db, analysis: None)
    # queued -> validating -> preprocessing -> ocr -> extracting
    for _ in range(4):
        advance_analysis(db, analysis)
    db.commit()
    assert analysis.state is AnalysisState.EXTRACTING
    return org, analysis


def _configure(monkeypatch, provider, *, api_key: str = "test-key") -> None:
    settings = Settings(
        secret_key="x" * 40, database_url="sqlite://", llm_api_key=api_key
    )
    monkeypatch.setattr("app.platform.config.get_settings", lambda: settings)
    if provider is not None:
        monkeypatch.setattr("app.extraction.llm.build_provider", lambda _s: provider)


class TestExtractingStage:
    def test_a_successful_extraction_advances_and_records_token_cost(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, _StubProvider())

        new_state = advance_analysis(db, analysis)
        db.commit()

        # P3-T6: evidence verification is its own stage now, immediately
        # after extraction and before normalizing - not folded into this
        # one, so a successful extraction lands here, not `normalizing`.
        assert new_state is AnalysisState.EVIDENCE_VERIFICATION
        assert analysis.total_tokens_in == 120
        assert analysis.total_tokens_out == 80

    def test_a_missing_credential_is_a_permanent_failure(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        """No key means retrying the identical input against the identical
        configuration cannot ever succeed - so it must dead-letter at once
        rather than burn three attempts."""
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, None, api_key="")

        with pytest.raises(PermanentStageError):
            advance_analysis(db, analysis)

    def test_a_provider_not_configured_error_is_permanent(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, None)

        def _raise(_settings):
            raise ProviderNotConfigured("no provider")

        monkeypatch.setattr("app.extraction.llm.build_provider", _raise)
        with pytest.raises(PermanentStageError):
            advance_analysis(db, analysis)

    def test_a_provider_transport_failure_is_transient(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        """A rate limit or 5xx is worth retrying with backoff - unlike a bad
        credential, the same input may well succeed on a later attempt."""
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, _RaisingProvider(ProviderError("429 rate limited")))

        with pytest.raises(TransientStageError):
            advance_analysis(db, analysis)

    def test_an_unparseable_response_is_a_permanent_failure(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, _StubProvider(text='{"nope": true}'))

        with pytest.raises(PermanentStageError):
            advance_analysis(db, analysis)

    def test_a_failed_stage_leaves_the_analysis_exactly_where_it_was(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        org, analysis = analysis_at_extracting
        _configure(monkeypatch, _RaisingProvider(ProviderError("boom")))

        with pytest.raises(TransientStageError):
            advance_analysis(db, analysis)
        db.rollback()
        assert analysis.state is AnalysisState.EXTRACTING

    def test_extracting_is_no_longer_a_placeholder(self) -> None:
        from app.analysis.stages import _placeholder

        assert STAGE_FUNCTIONS[AnalysisState.EXTRACTING] is not _placeholder


class TestEvidenceVerificationStageThroughTheRealPipeline:
    """P3-T6, exercised through the real orchestrator - `advance_analysis`
    twice, not the verification logic called directly - proving the stage
    is actually wired between `extracting` and `normalizing`, not just that
    `app.extraction.evidence.verify_extraction` works in isolation (see
    `tests/unit/test_extraction_evidence.py` and
    `tests/integration/test_extraction_verification.py` for that).

    `analysis_at_extracting`'s own fixture has exactly 2 real OCR tokens
    (`"Wheat flour"` index 0, `"250 g"` index 1) - `VALID_JSON` (P3-T5's own
    shared fixture) cites index 1 for `quantity_net_quantity` (a real,
    matching citation) and index 2 for `allergens_declaration_text`, which
    does not exist in this 2-token fixture - a naturally-occurring forged/
    out-of-range citation, not a hand-crafted adversarial case, giving one
    verified and one demoted field from the exact same real run.
    """

    def test_verification_runs_as_its_own_stage_after_extraction(
        self, db, analysis_at_extracting, monkeypatch
    ) -> None:
        from app.extraction.models import EvidenceSpan, ExtractedField, Extraction

        org, analysis = analysis_at_extracting
        _configure(monkeypatch, _StubProvider())

        extracting_result = advance_analysis(db, analysis)
        db.commit()
        assert extracting_result is AnalysisState.EVIDENCE_VERIFICATION

        extraction = db.scalar(select(Extraction).where(Extraction.analysis_id == analysis.id))
        quantity_field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        # Not yet verified: `_extracting` alone no longer runs the gate.
        assert quantity_field.verified is None
        assert extraction.verified_field_count == 0
        assert extraction.demoted_field_count == 0

        verification_result = advance_analysis(db, analysis)
        db.commit()
        assert verification_result is AnalysisState.NORMALIZING

        db.refresh(extraction)
        db.refresh(quantity_field)
        assert quantity_field.verified is True
        # `VALID_JSON` (P3-T5's own shared fixture) carries 5 fields with a
        # real value; only `quantity_net_quantity` cites a token that both
        # exists in this 2-token fixture AND textually matches - every other
        # populated field cites either an out-of-range index (demoted, no
        # citation resolves) or a real index whose text doesn't match
        # closely enough, so the other 4 are demoted too. Real, not
        # hand-tuned to make exactly one thing fail.
        assert extraction.verified_field_count == 1
        assert extraction.demoted_field_count == 4

        allergens_field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == extraction.id,
                ExtractedField.field_path == "allergens.declaration_text",
            )
        )
        assert allergens_field.verified is False

        span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == quantity_field.id)
        )
        assert span is not None
        assert span.text_snippet == "250 g"
        no_span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == allergens_field.id)
        )
        assert no_span is None

        facts = facts_schema.LabelFacts.model_validate(extraction.payload)
        assert facts.quantity.net_quantity.value == "250 g"
        assert facts.allergens.declaration_text.value is None

    def test_a_missing_extraction_is_a_permanent_failure(self, db, analysis_at_extracting) -> None:
        from app.analysis.stages import _evidence_verification

        _org, analysis = analysis_at_extracting
        analysis.state = AnalysisState.EVIDENCE_VERIFICATION
        db.flush()
        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _evidence_verification(db, analysis)
