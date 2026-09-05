"""Unit tests for the evidence verification gate's pure logic (P3-T6):
the fuzzy partial-ratio matcher and the per-field-path demotion mapping.
No database needed - `verify_extraction`'s DB-touching half has its own
integration suite in `tests/integration/test_extraction_verification.py`.
"""

from __future__ import annotations

import pytest

from app.extraction import facts as facts_schema
from app.extraction.evidence import DEMOTION_REASON, _demote, _normalize, partial_ratio

pytestmark = pytest.mark.unit


class TestNormalize:
    def test_casefolds(self) -> None:
        assert _normalize("WHEAT") == _normalize("wheat")

    def test_collapses_whitespace(self) -> None:
        assert _normalize("250   g") == _normalize("250 g")

    def test_strips_leading_and_trailing_whitespace(self) -> None:
        assert _normalize("  250 g  ") == "250 g"


class TestPartialRatio:
    def test_an_exact_match_scores_one(self) -> None:
        assert partial_ratio("250 g", "250 g") == 1.0

    def test_case_difference_alone_still_scores_one(self) -> None:
        """'Legitimate normalizations (case, spacing, unit)' (the task's own
        test line) must never cost a match."""
        assert partial_ratio("wheat", "WHEAT") == 1.0

    def test_spacing_difference_alone_still_scores_high(self) -> None:
        assert partial_ratio("250g", "250 g") >= 0.85

    def test_the_value_as_a_substring_of_a_longer_cited_line_scores_high(self) -> None:
        """The real case this function exists for: a citation covering a
        whole printed line ('Net Quantity: 250 g') containing a much
        shorter value ('250 g') must not be penalised for the surrounding
        text - a plain full-string ratio would score this far below 0.85."""
        assert partial_ratio("250 g", "Net Quantity: 250 g") >= 0.85

    def test_a_fabricated_value_unrelated_to_the_cited_text_scores_low(self) -> None:
        assert partial_ratio("500 kg", "Net Quantity: 250 g") < 0.85

    def test_completely_different_strings_score_low(self) -> None:
        assert partial_ratio("12/2027", "Wheat flour") < 0.85

    def test_an_empty_value_scores_zero(self) -> None:
        assert partial_ratio("", "Net Quantity: 250 g") == 0.0

    def test_an_empty_cited_text_scores_zero(self) -> None:
        assert partial_ratio("250 g", "") == 0.0

    def test_is_symmetric_in_which_string_is_longer(self) -> None:
        # The function always aligns the shorter string against the longer
        # one internally - proving it doesn't matter which argument is which.
        a = partial_ratio("250 g", "Net Quantity: 250 g")
        b = partial_ratio("Net Quantity: 250 g", "250 g")
        assert a == b


class TestDemote:
    """Every dotted field path P3-T5 actually persists must have a
    corresponding demotion case - this is the exhaustiveness check."""

    def _base_facts(self) -> facts_schema.LabelFacts:
        found_text = facts_schema.Fact.found("x")
        found_strs: facts_schema.Fact[list[str]] = facts_schema.Fact.found(["x"])
        return facts_schema.LabelFacts(
            ingredients=facts_schema.IngredientsFacts(
                declared_text=found_text,
                items=facts_schema.Fact.found(
                    [facts_schema.IngredientItem(name="Wheat", position=0)]
                ),
            ),
            allergens=facts_schema.AllergensFacts(
                declaration_text=found_text, declared=found_strs
            ),
            nutrition=facts_schema.NutritionFacts(
                serving_size=found_text,
                rows=facts_schema.Fact.found(
                    [facts_schema.NutritionRow(nutrient="Energy", unit="kcal")]
                ),
            ),
            quantity=facts_schema.QuantityFacts(net_quantity=found_text),
            dates=facts_schema.DatesFacts(
                manufacture_date=found_text,
                expiry_or_best_before=found_text,
                batch_number=found_text,
            ),
            claims=facts_schema.ClaimsFacts(
                items=facts_schema.Fact.found([facts_schema.Claim(text="x")])
            ),
            addresses=facts_schema.AddressesFacts(
                items=facts_schema.Fact.found(
                    [facts_schema.Address(role="manufacturer", text="x")]
                )
            ),
            languages=facts_schema.LanguagesFacts(detected=found_strs),
        )

    @pytest.mark.parametrize(
        "field_path",
        [
            "ingredients.declared_text",
            "allergens.declaration_text",
            "allergens.declared",
            "nutrition.serving_size",
            "nutrition.rows",
            "quantity.net_quantity",
            "dates.manufacture_date",
            "dates.expiry_or_best_before",
            "dates.batch_number",
            "claims.items",
            "addresses.items",
            "languages.detected",
        ],
    )
    def test_demoting_replaces_only_the_named_field(self, field_path: str) -> None:
        facts = self._base_facts()
        demoted = _demote(facts, field_path)

        section, attr = field_path.split(".")
        demoted_fact = getattr(getattr(demoted, section), attr)
        assert demoted_fact.value is None
        assert demoted_fact.not_found_reason == DEMOTION_REASON

        # Every other top-level section is untouched.
        for other_section in (
            "ingredients",
            "allergens",
            "nutrition",
            "quantity",
            "dates",
            "claims",
            "addresses",
            "languages",
        ):
            if other_section == section:
                continue
            assert getattr(demoted, other_section) == getattr(facts, other_section)

    def test_an_unrecognized_field_path_raises(self) -> None:
        with pytest.raises(ValueError, match="Unknown field_path"):
            _demote(self._base_facts(), "not.a.real.path")
