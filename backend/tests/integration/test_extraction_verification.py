"""Integration tests for the evidence verification gate (P3-T6), now its own
orchestrator stage (`app.analysis.stages._evidence_verification`) run
*after* `extract_for_analysis` rather than a side effect folded into it:
well-cited values are verified and get a real `EvidenceSpan`; hallucinated,
missing, forged, cross-page, or textually-unsupported citations are demoted
- in the persisted `LabelFacts` payload itself, not just an ignorable flag -
and counted. Uses the exact same OCR-token fixture as
`test_extraction_service.py` (P3-T5's own suite) so citation indices line up
the same way.

Every test here follows the same two real steps a live analysis actually
takes: `extract_for_analysis` (persists the raw, unverified fields), then
`_evidence_verification` (the exact function `app.analysis.stages` calls
from the orchestrator, not a hand-rolled equivalent) - proving the gate
works as it will actually run, not as an isolated unit.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis.retry_policy import PermanentStageError
from app.analysis.stages import _evidence_verification
from app.catalog.models import FilePage
from app.extraction import facts as facts_schema
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.vision.models import OcrTokenRow
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


def _extract_then_verify(db, org, version, analysis, provider):
    """The real two-stage sequence, not a shortcut: `extract_for_analysis`
    persists raw fields exactly like `_extracting` does, then
    `_evidence_verification` - the literal function the orchestrator calls -
    is run against that same analysis, mirroring `advance_analysis` without
    needing the analysis parked in the right `state` first (neither function
    reads `analysis.state`)."""
    outcome = _run(db, org, version, analysis, provider)
    db.commit()
    _evidence_verification(db, analysis)
    db.commit()
    return outcome


# Fixture tokens (from `analysis_with_tokens`, shared with test_extraction_service.py):
# [0] "Wheat flour"  [1] "250 g"  [2] "Contains: Wheat"  (all on one page)


def _field(db, extraction_id, field_path) -> ExtractedField:
    return db.scalar(
        select(ExtractedField).where(
            ExtractedField.extraction_id == extraction_id,
            ExtractedField.field_path == field_path,
        )
    )


class TestValidCitationsAreVerified:
    def test_a_matching_citation_is_verified_and_gets_an_evidence_span(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [1], "confidence": 0.99
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is True
        assert field.match_ratio == 1.0

        db.refresh(outcome.extraction)
        assert outcome.extraction.verified_field_count == 1
        assert outcome.extraction.demoted_field_count == 0

        span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == field.id)
        )
        assert span is not None
        assert span.text_snippet == "250 g"
        assert span.source == "ocr"
        assert span.x1 == 10.0  # token 1's own bbox, from the shared fixture

        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value == "250 g"

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
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        field = _field(db, outcome.extraction.id, "allergens.declaration_text")
        assert field.verified is True
        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.allergens.declaration_text.value == "Contains: Wheat"

    def test_legitimate_case_and_spacing_differences_still_verify(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250G", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is True
        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value == "250G"

    def test_partial_evidence_still_verifies_when_the_valid_tokens_are_enough(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """A mix of one real, matching citation and one forged/out-of-range
        index for the same field - `_resolve_citations` (P3-T5) already
        drops the unresolvable index before this gate ever sees it, so
        verification runs against whatever real tokens remain. This is not
        a loophole: the surviving real token still has to actually support
        the value on its own, which it does here."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None,
                "token_ids": [1, 99], "confidence": 0.9,
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is True
        db.refresh(outcome.extraction)
        assert outcome.extraction.verified_field_count == 1


class TestInvalidCitationsAreDemoted:
    def test_a_value_unrelated_to_its_citation_is_demoted_in_the_payload(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """The literal acceptance criterion: the corrected `LabelFacts` -
        what `rule_eval` will eventually read - no longer carries the
        hallucinated value at all, not just a flag next to it. This is the
        "mismatched text" / contradictory-citation case."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "500 kg", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        db.refresh(outcome.extraction)
        assert outcome.extraction.demoted_field_count == 1
        assert outcome.extraction.verified_field_count == 0

        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value is None
        assert "could not be verified" in payload_facts.quantity.net_quantity.not_found_reason

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
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

    def test_an_empty_citation_list_is_demoted(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """"Missing citation" / "empty evidence": a value with no cited
        tokens at all cannot pass, however plausible the value looks."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None, "token_ids": [], "confidence": 0.9
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        db.refresh(outcome.extraction)
        assert outcome.extraction.demoted_field_count == 1
        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value is None

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is False
        assert field.match_ratio == 0.0

    def test_a_forged_out_of_range_citation_is_demoted(
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
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        db.refresh(outcome.extraction)
        assert outcome.extraction.demoted_field_count == 1
        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value is None

    def test_a_not_found_field_is_neither_verified_nor_demoted(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """An honest absence is not a verification failure - it never had a
        value to check in the first place."""
        org, version, analysis = analysis_with_tokens
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(_envelope()))

        db.refresh(outcome.extraction)
        assert outcome.extraction.verified_field_count == 0
        assert outcome.extraction.demoted_field_count == 0

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is None
        assert field.match_ratio is None


class TestCrossPageCitationsAreDemoted:
    """"Wrong-page citation": a field whose cited tokens resolve to real
    rows, with text that even matches, but the tokens come from two
    different pages - a contradiction a real printed field's evidence
    cannot legitimately produce, and must never be silently accepted just
    because each individual token is real."""

    @pytest.fixture
    def analysis_with_tokens_on_two_pages(self, db, analysis_with_tokens):  # noqa: F811
        org, version, analysis = analysis_with_tokens
        first_page_token = db.scalar(select(OcrTokenRow).where(OcrTokenRow.text == "250 g"))
        first_page = db.get(FilePage, first_page_token.file_page_id)
        second_page = FilePage(
            organization_id=org.id,
            file_id=first_page.file_id,
            page_no=2,
            width=800,
            height=600,
            render_key="r2",
        )
        db.add(second_page)
        db.flush()
        second_page_token = OcrTokenRow(
            organization_id=org.id,
            file_page_id=second_page.id,
            ocr_result_id=first_page_token.ocr_result_id,
            text="250 g",
            confidence=0.9,
            x1=0.0,
            y1=0.0,
            x2=5.0,
            y2=10.0,
            line_no=0,
        )
        db.add(second_page_token)
        db.flush()
        return org, version, analysis, first_page_token, second_page_token

    def test_citations_spanning_two_pages_are_demoted_even_with_matching_text(
        self, db, analysis_with_tokens_on_two_pages
    ) -> None:
        org, version, analysis, first_token, second_token = analysis_with_tokens_on_two_pages
        envelope = _envelope(
            quantity_net_quantity={
                "value": "250 g", "not_found_reason": None,
                "token_ids": [1, 3], "confidence": 0.9,
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        db.refresh(outcome.extraction)
        assert outcome.extraction.demoted_field_count == 1
        assert outcome.extraction.verified_field_count == 0

        field = _field(db, outcome.extraction.id, "quantity.net_quantity")
        assert field.verified is False
        assert field.match_ratio == 0.0
        payload_facts = facts_schema.LabelFacts.model_validate(outcome.extraction.payload)
        assert payload_facts.quantity.net_quantity.value is None
        assert (
            db.scalar(select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == field.id))
            is None
        )


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
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        db.refresh(outcome.extraction)
        assert outcome.extraction.verified_field_count == 1
        assert outcome.extraction.demoted_field_count == 1

    def test_the_final_persisted_payload_reflects_the_demotion(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        """Not just an in-memory object - the actual `Extraction.payload`
        column, since that is what a later stage (`_normalizing`,
        `_classifying`, eventually `rule_eval`) re-reads from the database,
        not the original in-process object."""
        org, version, analysis = analysis_with_tokens
        envelope = _envelope(
            quantity_net_quantity={
                "value": "500 kg", "not_found_reason": None, "token_ids": [1], "confidence": 0.9
            }
        )
        outcome = _extract_then_verify(db, org, version, analysis, _StubProvider(envelope))

        stored = db.scalar(select(Extraction).where(Extraction.id == outcome.extraction.id))
        payload_facts = facts_schema.LabelFacts.model_validate(stored.payload)
        assert payload_facts.quantity.net_quantity.value is None


class TestVerificationStageFailureHandling:
    def test_verifying_with_no_extraction_is_a_permanent_failure(
        self, db, analysis_with_tokens  # noqa: F811
    ) -> None:
        _org, _version, analysis = analysis_with_tokens
        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _evidence_verification(db, analysis)
