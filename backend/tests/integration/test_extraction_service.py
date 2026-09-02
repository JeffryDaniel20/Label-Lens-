"""Extraction orchestration against a stubbed provider (P3-T5).

P3-T5's test line calls for "stubbed-provider integration" and a
"schema-failure repair path"; its acceptance criterion is that extraction
"returns a valid fact object with citations, or fails explicitly - never
partially valid". The never-partial half is the important one and is
asserted directly: after an exhausted run, no `Extraction` row exists at all.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis import service as analysis_service
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction import service
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.extraction.llm.prompt import DELIMITER_OPEN
from app.extraction.models import ExtractedField, Extraction
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

pytestmark = pytest.mark.integration

VALID_JSON = """
{
  "ingredients_declared_text": {"value": "Wheat flour, Sugar", "not_found_reason": null,
                                "token_ids": [0, 1], "confidence": 0.95},
  "allergens_declaration_text": {"value": "Contains: Wheat", "not_found_reason": null,
                                 "token_ids": [2], "confidence": 0.9},
  "allergens_declared": {"values": ["Wheat"], "not_found_reason": null,
                         "token_ids": [2], "confidence": 0.9},
  "nutrition_serving_size": {"value": null, "not_found_reason": "not printed",
                             "token_ids": [], "confidence": 0.0},
  "nutrition_rows": [],
  "nutrition_rows_not_found_reason": "no nutrition panel on these pages",
  "quantity_net_quantity": {"value": "250 g", "not_found_reason": null,
                            "token_ids": [1], "confidence": 0.99},
  "dates_manufacture": {"value": null, "not_found_reason": "not printed",
                        "token_ids": [], "confidence": 0.0},
  "dates_expiry_or_best_before": {"value": null, "not_found_reason": "not printed",
                                  "token_ids": [], "confidence": 0.0},
  "dates_batch_number": {"value": null, "not_found_reason": "not printed",
                         "token_ids": [], "confidence": 0.0},
  "claims": [],
  "claims_not_found_reason": "no claims printed",
  "addresses": [],
  "addresses_not_found_reason": "no address printed",
  "languages_detected": {"values": ["en"], "not_found_reason": null,
                         "token_ids": [], "confidence": 0.9}
}
"""

INVALID_JSON = '{"ingredients_declared_text": "this should be an object, not a string"}'


class _StubProvider:
    """Returns scripted responses and records exactly what it was asked."""

    name = "stub"

    def __init__(self, *responses: str) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, object]] = []

    def generate(self, *, system_instruction, prompt, schema, model) -> ProviderResponse:
        self.calls.append(
            {"system_instruction": system_instruction, "prompt": prompt, "model": model}
        )
        text = self._responses[min(len(self.calls) - 1, len(self._responses) - 1)]
        return ProviderResponse(
            text=text, usage=ProviderUsage(tokens_in=100, tokens_out=50), model=model
        )


@pytest.fixture
def analysis_with_tokens(db):
    """A real analysis whose product version has real OCR tokens."""
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
        sha256="a" * 64,
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
    for i, text in enumerate(("Wheat flour", "250 g", "Contains: Wheat")):
        db.add(
            OcrTokenRow(
                organization_id=org.id,
                file_page_id=page.id,
                ocr_result_id=result.id,
                text=text,
                confidence=0.9,
                x1=float(i * 10),
                y1=0.0,
                x2=float(i * 10 + 5),
                y2=10.0,
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
    db.commit()
    return org, version, analysis


def _run(db, org, version, analysis, provider):
    return service.extract_for_analysis(
        db,
        provider=provider,
        organization_id=org.id,
        analysis_id=analysis.id,
        product_version_id=version.id,
        model="stub-flash",
        escalation_model="stub-pro",
    )


class TestHappyPath:
    def test_a_valid_response_persists_an_extraction(self, db, analysis_with_tokens) -> None:
        org, version, analysis = analysis_with_tokens
        provider = _StubProvider(VALID_JSON)

        outcome = _run(db, org, version, analysis, provider)
        db.commit()

        assert outcome.extraction.attempts == 1
        assert outcome.extraction.escalated is False
        assert outcome.extraction.provider == "stub"
        assert outcome.extraction.schema_version == "1.0.0"
        assert outcome.extraction.prompt_hash
        assert outcome.label_facts.quantity.net_quantity.value == "250 g"

    def test_per_field_rows_are_written_with_resolved_citations(
        self, db, analysis_with_tokens
    ) -> None:
        org, version, analysis = analysis_with_tokens
        outcome = _run(db, org, version, analysis, _StubProvider(VALID_JSON))
        db.commit()

        rows = db.scalars(
            select(ExtractedField).where(
                ExtractedField.extraction_id == outcome.extraction.id
            )
        ).all()
        by_path = {r.field_path: r for r in rows}

        assert "quantity.net_quantity" in by_path
        assert by_path["quantity.net_quantity"].value_raw == "250 g"
        # Citations resolved from prompt indices to real ocr_tokens.id values.
        cited = by_path["quantity.net_quantity"].cited_token_ids
        assert len(cited) == 1
        token_ids = {
            str(t.id)
            for t in db.scalars(
                select(OcrTokenRow).where(OcrTokenRow.organization_id == org.id)
            ).all()
        }
        assert set(cited) <= token_ids

    def test_an_absent_field_records_its_reason_not_a_guess(
        self, db, analysis_with_tokens
    ) -> None:
        org, version, analysis = analysis_with_tokens
        outcome = _run(db, org, version, analysis, _StubProvider(VALID_JSON))
        db.commit()

        row = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == outcome.extraction.id,
                ExtractedField.field_path == "dates.manufacture_date",
            )
        )
        assert row.value_raw is None
        assert row.not_found_reason == "not printed"
        assert row.cited_token_ids == []

    def test_the_provider_receives_the_untrusted_framed_prompt(
        self, db, analysis_with_tokens
    ) -> None:
        org, version, analysis = analysis_with_tokens
        provider = _StubProvider(VALID_JSON)
        _run(db, org, version, analysis, provider)

        prompt = provider.calls[0]["prompt"]
        assert DELIMITER_OPEN in prompt
        assert "Wheat flour" in prompt
        assert "UNTRUSTED DATA" in provider.calls[0]["system_instruction"]


class TestRepairAndEscalation:
    def test_one_schema_failure_is_repaired_on_the_second_attempt(
        self, db, analysis_with_tokens
    ) -> None:
        org, version, analysis = analysis_with_tokens
        provider = _StubProvider(INVALID_JSON, VALID_JSON)

        outcome = _run(db, org, version, analysis, provider)
        db.commit()

        assert outcome.extraction.attempts == 2
        assert outcome.extraction.escalated is False
        # The retry is a correction, not a fresh guess: the validation error
        # is fed back verbatim.
        assert "did not satisfy the required schema" in provider.calls[1]["prompt"]
        # Still the cheap model - escalation only happens after the repair fails.
        assert provider.calls[1]["model"] == "stub-flash"

    def test_a_failed_repair_escalates_the_model_tier(self, db, analysis_with_tokens) -> None:
        org, version, analysis = analysis_with_tokens
        provider = _StubProvider(INVALID_JSON, INVALID_JSON, VALID_JSON)

        outcome = _run(db, org, version, analysis, provider)
        db.commit()

        assert outcome.extraction.attempts == 3
        assert outcome.extraction.escalated is True
        assert provider.calls[0]["model"] == "stub-flash"
        assert provider.calls[2]["model"] == "stub-pro"

    def test_usage_accumulates_across_every_attempt(self, db, analysis_with_tokens) -> None:
        org, version, analysis = analysis_with_tokens
        outcome = _run(
            db, org, version, analysis, _StubProvider(INVALID_JSON, VALID_JSON)
        )
        # Two calls really happened, so two calls' worth of tokens are billed.
        assert outcome.tokens_in == 200
        assert outcome.tokens_out == 100
        assert outcome.extraction.tokens_in == 200


class TestExplicitFailureNeverPartial:
    def test_exhausting_every_attempt_raises(self, db, analysis_with_tokens) -> None:
        org, version, analysis = analysis_with_tokens
        provider = _StubProvider(INVALID_JSON)

        with pytest.raises(service.ExtractionFailed):
            _run(db, org, version, analysis, provider)
        assert len(provider.calls) == service.MAX_ATTEMPTS

    def test_a_failed_extraction_persists_absolutely_nothing(
        self, db, analysis_with_tokens
    ) -> None:
        """The acceptance criterion: never partially valid."""
        org, version, analysis = analysis_with_tokens

        with pytest.raises(service.ExtractionFailed):
            _run(db, org, version, analysis, _StubProvider(INVALID_JSON))
        db.rollback()

        assert db.scalars(select(Extraction)).all() == []
        assert db.scalars(select(ExtractedField)).all() == []

    def test_an_analysis_with_no_ocr_tokens_fails_explicitly(self, db) -> None:
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
                sha256="b" * 64,
                mime="image/jpeg",
                bytes=1,
                status=FileStatus.READY,
            )
        )
        db.flush()
        file_hash = analysis_service.compute_file_set_hash(
            db, organization_id=org.id, version_id=version.id
        )
        analysis, _ = analysis_service.create_or_get_analysis(
            db, organization_id=org.id, version=version, file_set_hash=file_hash
        )
        db.commit()

        with pytest.raises(service.ExtractionFailed, match="No OCR tokens"):
            _run(db, org, version, analysis, _StubProvider(VALID_JSON))


class TestTenantIsolation:
    def test_another_orgs_tokens_are_never_loaded(self, db, analysis_with_tokens) -> None:
        org, version, _analysis = analysis_with_tokens
        other = make_org(db, name="Beta Foods")
        db.commit()

        assert service.load_ocr_tokens(
            db, organization_id=org.id, product_version_id=version.id
        )
        assert (
            service.load_ocr_tokens(
                db, organization_id=other.id, product_version_id=version.id
            )
            == []
        )

    def test_tokens_load_in_reading_order(self, db, analysis_with_tokens) -> None:
        org, version, _analysis = analysis_with_tokens
        tokens = service.load_ocr_tokens(
            db, organization_id=org.id, product_version_id=version.id
        )
        assert [t.text for t in tokens] == ["Wheat flour", "250 g", "Contains: Wheat"]
