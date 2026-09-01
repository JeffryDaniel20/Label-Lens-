"""Unit tests for category/jurisdiction classification (P3-T9).

The central acceptance criterion under test is IMPLEMENTATION.md's own:
"no analysis proceeds to rules with a guessed category." Every abstention
test proves the classifier refuses rather than guesses; every
confident-routing test proves it doesn't refuse when the signal genuinely
supports a decision. No live services needed - pure Python over
`LabelFacts`.
"""

from __future__ import annotations

import pytest

from app.classification.classifier import (
    CANONICAL_CATEGORIES,
    CLASSIFIER_VERSION,
    SUPPORTED_JURISDICTIONS,
    UserHints,
    classify,
)
from app.extraction.facts import (
    Address,
    AddressesFacts,
    AllergensFacts,
    ClaimsFacts,
    DatesFacts,
    Fact,
    IngredientItem,
    IngredientsFacts,
    LabelFacts,
    LanguagesFacts,
    NutritionFacts,
    NutritionRow,
    QuantityFacts,
)

pytestmark = pytest.mark.unit

_REASON = "not present in this fixture"


def _blank_kwargs() -> dict[str, object]:
    return {
        "ingredients": IngredientsFacts(
            declared_text=Fact.missing(_REASON), items=Fact.missing(_REASON)
        ),
        "allergens": AllergensFacts(
            declaration_text=Fact.missing(_REASON), declared=Fact.missing(_REASON)
        ),
        "nutrition": NutritionFacts(
            serving_size=Fact.missing(_REASON), rows=Fact.missing(_REASON)
        ),
        "quantity": QuantityFacts(net_quantity=Fact.missing(_REASON)),
        "dates": DatesFacts(
            manufacture_date=Fact.missing(_REASON),
            expiry_or_best_before=Fact.missing(_REASON),
            batch_number=Fact.missing(_REASON),
        ),
        "claims": ClaimsFacts(items=Fact.missing(_REASON)),
        "addresses": AddressesFacts(items=Fact.missing(_REASON)),
        "languages": LanguagesFacts(detected=Fact.missing(_REASON)),
    }


def _blank_facts() -> LabelFacts:
    """The "unrelated image (a cat)" fabrication-bait case: every fact is
    explicitly absent."""
    return LabelFacts(**_blank_kwargs())  # type: ignore[arg-type]


def _facts_with_n_core_signals(n: int, *, address_text: str | None = None) -> LabelFacts:
    kwargs = _blank_kwargs()
    if n >= 1:
        kwargs["ingredients"] = IngredientsFacts(
            declared_text=Fact.found("Wheat Flour, Sugar, Salt"),
            items=Fact.found([IngredientItem(name="Wheat Flour", position=0)]),
        )
    if n >= 2:
        kwargs["nutrition"] = NutritionFacts(
            serving_size=Fact.found("30 g"),
            rows=Fact.found([NutritionRow(nutrient="energy", unit="kcal", per_100g=380.0)]),
        )
    if n >= 3:
        kwargs["quantity"] = QuantityFacts(net_quantity=Fact.found("250 g"))
    if address_text is not None:
        kwargs["addresses"] = AddressesFacts(
            items=Fact.found([Address(role="manufacturer", text=address_text)])
        )
    return LabelFacts(**kwargs)  # type: ignore[arg-type]


def _full_facts(*, address_text: str | None = None) -> LabelFacts:
    return _facts_with_n_core_signals(3, address_text=address_text)


class TestAbstention:
    def test_abstains_on_a_completely_blank_label(self) -> None:
        result = classify(_blank_facts())
        assert result.abstained is True
        assert result.category is None
        assert result.jurisdictions == ()
        assert result.reason is not None and "Category confidence" in result.reason

    def test_abstains_with_only_one_of_three_core_signals(self) -> None:
        result = classify(_facts_with_n_core_signals(1), UserHints(market_codes=("IN",)))
        assert result.abstained is True
        assert result.category is None

    def test_unrecognized_category_hint_forces_abstention_even_with_full_facts(self) -> None:
        result = classify(
            _full_facts(address_text="123 MG Road, Mumbai, India"),
            UserHints(category_hint="cosmetics", market_codes=("IN",)),
        )
        assert result.abstained is True
        assert result.reason == "Unrecognized category hint: 'cosmetics'."

    def test_no_jurisdiction_signal_abstains_even_with_confident_category(self) -> None:
        result = classify(_full_facts())  # no address, no hints at all
        assert result.abstained is True
        assert result.category is None  # never a category with no jurisdiction
        assert result.reason is not None and "Jurisdiction confidence" in result.reason

    def test_never_returns_a_category_when_abstaining(self) -> None:
        """The literal acceptance criterion: abstaining never leaves a
        guessed category or jurisdiction sitting in the result."""
        for result in (
            classify(_blank_facts()),
            classify(_full_facts()),
            classify(_full_facts(), UserHints(category_hint="not_a_category")),
        ):
            if result.abstained:
                assert result.category is None
                assert result.jurisdictions == ()


class TestCorrectRouting:
    def test_full_facts_with_an_in_market_code_routes_confidently(self) -> None:
        result = classify(_full_facts(), UserHints(market_codes=("IN",)))
        assert result.abstained is False
        assert result.category == "packaged_food"
        assert result.jurisdictions == ("IN",)
        assert result.category_confidence == pytest.approx(1.0)
        assert result.jurisdiction_confidence == pytest.approx(1.0)

    def test_full_facts_with_an_eu_market_code_routes_confidently(self) -> None:
        # Proves no hardcoded preference for either jurisdiction (D-01 is
        # not resolved or presumed by this classifier).
        result = classify(_full_facts(), UserHints(market_codes=("EU",)))
        assert result.abstained is False
        assert result.jurisdictions == ("EU",)

    def test_multiple_recognized_market_codes_all_route(self) -> None:
        result = classify(_full_facts(), UserHints(market_codes=("IN", "EU")))
        assert result.abstained is False
        assert set(result.jurisdictions) == {"IN", "EU"}
        assert result.jurisdiction_confidence == pytest.approx(1.0)

    def test_partially_recognized_market_codes_still_route_with_reduced_confidence(
        self,
    ) -> None:
        result = classify(_full_facts(), UserHints(market_codes=("IN", "US")))
        assert result.abstained is False
        assert result.jurisdictions == ("IN",)
        assert result.jurisdiction_confidence == pytest.approx(0.6)

    def test_lowercase_market_codes_are_normalized(self) -> None:
        result = classify(_full_facts(), UserHints(market_codes=("in",)))
        assert result.jurisdictions == ("IN",)

    def test_entirely_unrecognized_market_codes_fall_back_to_address_inference(
        self,
    ) -> None:
        result = classify(
            _full_facts(address_text="123 MG Road, Mumbai, India"),
            UserHints(market_codes=("US",)),
        )
        assert result.abstained is False
        assert result.jurisdictions == ("IN",)
        assert result.jurisdiction_confidence == pytest.approx(0.5)

    def test_address_based_jurisdiction_inference_when_no_hint_given(self) -> None:
        result = classify(_full_facts(address_text="123 MG Road, Mumbai, India"))
        assert result.abstained is False
        assert result.jurisdictions == ("IN",)
        assert result.jurisdiction_confidence == pytest.approx(0.5)

    def test_a_category_hint_boosts_a_marginal_category_confidence_over_threshold(
        self,
    ) -> None:
        # 1 of 3 core signals alone (~0.33) would abstain; the recognized
        # hint combines with facts rather than being ignored.
        result = classify(
            _facts_with_n_core_signals(1),
            UserHints(category_hint="packaged_food", market_codes=("EU",)),
        )
        assert result.abstained is False
        assert result.category == "packaged_food"

    def test_exactly_two_of_three_core_signals_meets_the_threshold(self) -> None:
        result = classify(_facts_with_n_core_signals(2), UserHints(market_codes=("IN",)))
        assert result.abstained is False
        assert result.category_confidence == pytest.approx(2 / 3)


class TestVersionAndDeterminism:
    def test_classifier_version_is_a_stable_string(self) -> None:
        assert isinstance(CLASSIFIER_VERSION, str)
        assert CLASSIFIER_VERSION == "1.0.0"

    def test_classification_is_deterministic(self) -> None:
        facts = _full_facts(address_text="123 MG Road, Mumbai, India")
        hints = UserHints(market_codes=("IN",))
        results = {classify(facts, hints) for _ in range(10)}
        assert len(results) == 1

    def test_recognized_categories_and_jurisdictions_are_frozen_sets(self) -> None:
        assert CANONICAL_CATEGORIES == frozenset({"packaged_food"})
        assert SUPPORTED_JURISDICTIONS == frozenset({"IN", "EU"})
