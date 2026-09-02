"""The `extracting` pipeline stage (P3-T5 wired into P5-T2/P5-T3).

Covers the seam between extraction and the queue: that a provider/credential
problem is classified into P5-T3's retry vocabulary correctly, and that real
provider spend lands on the analysis via P5-T5's cost accounting.
"""

from __future__ import annotations

import pytest

from app.analysis import service as analysis_service
from app.analysis.models import AnalysisState
from app.analysis.retry_policy import PermanentStageError, TransientStageError
from app.analysis.stages import STAGE_FUNCTIONS, advance_analysis
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
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
def analysis_at_extracting(db):
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

        assert new_state is AnalysisState.NORMALIZING
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
