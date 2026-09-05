"""Integration tests for the evidence verification gate wired into
`extract_for_analysis` (P3-T6): well-cited values are verified and get a
real `EvidenceSpan`; hallucinated/uncited values are demoted - in the
persisted `LabelFacts` payload itself, not just an ignorable flag - and
counted. Uses the exact same OCR-token fixture as
`test_extraction_service.py` (P3-T5's own suite) so citation indices line
up the same way.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.extraction import facts as facts_schema
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from tests.integration.test_extraction_service import _run, analysis_with_tokens  # noqa: F401

pytestmark = pytest.mark.integration


class _StubProvider:
    name = "stub"

    def __init__(self, text: str) -> None:
        self.text = text

    def generate(self, *, system_instruction, prompt, schema, model) -> ProviderResponse:
        return ProviderResponse(
            text=self.text, usage=ProviderUsage(tokens_in=10, tokens_out=5), model=model
        )


def _envelope(**overrides: object) -> str:
    import json

    empty_text: dict[str, object] = {
        "value": None, "not_found_reason": "not printed", "token_ids": [], "confidence": 0.0
    }
    empty_list: dict[str, object] = {
        "values": [], "not_found_reason": "not printed", "token_ids": [], "confidence": 0.0
    }
    base: dict[str, object] = {
        "ingredients_declared_text": empty_text,
        "allergens_declaration_text": empty_text,
        "allergens_declared": empty_list,
        "nutrition_serving_size": empty_text,
        "nutrition_rows": [],
        "nutrition_rows_not_found_reason": "not printed",
        "quantity_net_quantity": empty_text,
        "dates_manufacture": empty_text,
        "dates_expiry_or_best_before": empty_text,
        "dates_batch_number": empty_text,
        "claims": [],
        "claims_not_found_reason": "not printed",
        "addresses": [],
        "addresses_not_found_reason": "not printed",
        "languages_detected": empty_list,
    }
    base.update(overrides)
    return json.dumps(base)


# Fixture tokens (from `analysis_with_tokens`, shared with test_extraction_service.py):
# [0] "Wheat flour"  [1] "250 g"  [2] "Contains: Wheat"


class TestWellCitedValuesAreVerified:
    def test_a_matching_citation_is_verified_and_gets_an_evidence_span(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [1], "confidence": 0.99
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.verified_field_count == 1
        assert outcome.demoted_field_count == 0
        assert outcome.label_facts.quantity.net_quantity.value == "250 g"
        assert outcome.extraction.verified_field_count == 1
        assert outcome.extraction.demoted_field_count == 0

        field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == outcome.extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        assert field.verified is True
        assert field.match_ratio == 1.0

        span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == field.id)
        )
        assert span is not None
        assert span.text_snippet == "250 g"
        assert span.source == "ocr"
        assert span.x1 == 10.0  # token 1's own bbox, from the shared fixture

    def test_a_citation_covering_a_whole_line_still_verifies_a_shorter_value(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """The real-world case: the model cites the whole printed line
        ('Contains: Wheat') as the source for a shorter declared value."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            allergens_declaration_text={
                "value": "Contains: Wheat", "not_found_reason": None,
                "token_ids": [2], "confidence": 0.9,
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.label_facts.allergens.declaration_text.value == "Contains: Wheat"
        assert outcome.verified_field_count == 1

    def test_legitimate_case_and_spacing_differences_still_verify(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250G", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.verified_field_count == 1
        assert outcome.label_facts.quantity.net_quantity.value == "250G"


class TestHallucinatedValuesAreDemoted:
    def test_a_value_unrelated_to_its_citation_is_demoted_in_the_payload(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """The literal acceptance criterion: the corrected `LabelFacts` -
        what `rule_eval` will eventually read - no longer carries the
        hallucinated value at all, not just a flag next to it."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "500 kg", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.demoted_field_count == 1
        assert outcome.verified_field_count == 0
        assert outcome.label_facts.quantity.net_quantity.value is None
        assert (
            "could not be verified"
            in outcome.label_facts.quantity.net_quantity.not_found_reason
        )

        field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == outcome.extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        assert field.verified is False
        assert field.match_ratio < 0.85
        # The raw model output is still there for debugging - only the
        # *facts* payload is corrected, not the audit trail of what the
        # model actually said.
        assert field.value_raw == "500 kg"

        assert (
            db.scalar(select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == field.id))
            is None
        )

    def test_an_uncited_value_is_demoted(self, db, analysis_with_tokens) -> None:  # noqa: F811
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [], "confidence": 0.9
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.demoted_field_count == 1
        assert outcome.label_facts.quantity.net_quantity.value is None

    def test_a_citation_to_a_hallucinated_out_of_range_index_is_demoted(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """`app.extraction.service._resolve_citations` already drops an
        out-of-range index; this proves the field it belonged to still
        correctly ends up with zero real tokens and gets demoted, not
        silently left verified."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [99], "confidence": 0.9
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        assert outcome.demoted_field_count == 1
        assert outcome.label_facts.quantity.net_quantity.value is None

    def test_a_not_found_field_is_neither_verified_nor_demoted(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """An honest absence is not a verification failure - it never had a
        value to check in the first place."""
        org, version, analysis = analysis_with_tokens
        outcome = _run(db, org, version, analysis, _StubProvider(_envelope()))
        db.commit()

        assert outcome.verified_field_count == 0
        assert outcome.demoted_field_count == 0

        field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == outcome.extraction.id,
                ExtractedField.field_path == "quantity.net_quantity",
            )
        )
        assert field.verified is None
        assert field.match_ratio is None


class TestVerificationCountsAndPersistence:
    def test_counts_are_persisted_on_the_extraction_row(self, db, analysis_with_tokens) -> None:  # noqa: F811
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            },
            dates_manufacture={
                "value": "not on this label", "not_found_reason": None,
                "token_ids": [1], "confidence": 0.5,
            },
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        db.refresh(outcome.extraction)
        assert outcome.extraction.verified_field_count == 1
        assert outcome.extraction.demoted_field_count == 1

    def test_the_final_persisted_payload_reflects_the_demotion(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """Not just the in-memory `outcome.label_facts` - the actual
        `Extraction.payload` column, since that is what a later stage
        (`_normalizing`, `_classifying`, eventually `rule_eval`) re-reads
        from the database, not the original in-process object."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "500 kg", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _run(db, org, version, analysis, _StubProvider(envelope))
        db.commit()

        stored = db.scalar(select(Extraction).where(Extraction.id == outcome.extraction.id))
        payload_facts = facts_schema.LabelFacts.model_validate(stored.payload)
        assert payload_facts.quantity.net_quantity.value is None
