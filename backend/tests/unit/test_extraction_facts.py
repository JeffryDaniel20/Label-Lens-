"""Unit tests for the fact schema (P3-T4) - the frozen AI/rules contract.

No live services needed: this is pure Pydantic model behavior.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.extraction import facts as facts_module
from app.extraction.facts import (
    SCHEMA_VERSION,
    Address,
    AddressesFacts,
    AllergensFacts,
    Claim,
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


def _fully_populated_facts() -> LabelFacts:
    return LabelFacts(
        ingredients=IngredientsFacts(
            declared_text=Fact.found("Wheat Flour, Sugar, Salt"),
            items=Fact.found(
                [
                    IngredientItem(name="Wheat Flour", position=0),
                    IngredientItem(name="Sugar", position=1),
                    IngredientItem(name="Salt", position=2, percentage=1.5),
                ]
            ),
        ),
        allergens=AllergensFacts(
            declaration_text=Fact.found("Contains: Wheat"),
            declared=Fact.found(["Wheat"]),
        ),
        nutrition=NutritionFacts(
            serving_size=Fact.found("30 g"),
            rows=Fact.found(
                [NutritionRow(nutrient="energy", unit="kcal", per_100g=380.0, per_serving=114.0)]
            ),
        ),
        quantity=QuantityFacts(net_quantity=Fact.found("250 g")),
        dates=DatesFacts(
            manufacture_date=Fact.missing("No manufacture date printed on the label."),
            expiry_or_best_before=Fact.found("2027-12"),
            batch_number=Fact.missing("Batch code not legible in the photo."),
        ),
        claims=ClaimsFacts(items=Fact.found([Claim(text="No artificial colours")])),
        addresses=AddressesFacts(
            items=Fact.found([Address(role="manufacturer", text="123 Factory Rd, Mumbai")])
        ),
        languages=LanguagesFacts(detected=Fact.found(["en"])),
    )


def _all_not_found_facts() -> LabelFacts:
    """The "fabrication bait" case: a blank page or an unrelated photo - every
    fact is explicitly absent, never guessed."""
    reason = "The image does not appear to contain a product label."
    return LabelFacts(
        ingredients=IngredientsFacts(
            declared_text=Fact.missing(reason), items=Fact.missing(reason)
        ),
        allergens=AllergensFacts(
            declaration_text=Fact.missing(reason), declared=Fact.missing(reason)
        ),
        nutrition=NutritionFacts(serving_size=Fact.missing(reason), rows=Fact.missing(reason)),
        quantity=QuantityFacts(net_quantity=Fact.missing(reason)),
        dates=DatesFacts(
            manufacture_date=Fact.missing(reason),
            expiry_or_best_before=Fact.missing(reason),
            batch_number=Fact.missing(reason),
        ),
        claims=ClaimsFacts(items=Fact.missing(reason)),
        addresses=AddressesFacts(items=Fact.missing(reason)),
        languages=LanguagesFacts(detected=Fact.missing(reason)),
    )


class TestFactInvariant:
    def test_value_only_is_valid(self) -> None:
        fact = Fact[str](value="250 g")
        assert fact.value == "250 g"
        assert fact.not_found_reason is None

    def test_reason_only_is_valid(self) -> None:
        fact = Fact[str](not_found_reason="illegible")
        assert fact.value is None
        assert fact.not_found_reason == "illegible"

    def test_neither_value_nor_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="not_found_reason"):
            Fact[str]()

    def test_both_value_and_reason_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="mutually exclusive"):
            Fact[str](value="250 g", not_found_reason="illegible")

    def test_found_and_missing_helpers(self) -> None:
        assert Fact.found("x").value == "x"
        assert Fact.missing("why").not_found_reason == "why"

    def test_facts_are_frozen(self) -> None:
        fact = Fact.found("x")
        with pytest.raises(ValidationError):
            fact.value = "y"  # type: ignore[misc]


class TestLabelFactsRoundTrip:
    def test_fully_populated_facts_round_trip(self) -> None:
        original = _fully_populated_facts()
        restored = LabelFacts.model_validate_json(original.model_dump_json())
        assert restored == original

    def test_all_not_found_facts_round_trip(self) -> None:
        """The blank-page/unrelated-photo case still constructs and
        round-trips validly - "insufficient data" is a first-class,
        representable outcome, not an error."""
        original = _all_not_found_facts()
        restored = LabelFacts.model_validate_json(original.model_dump_json())
        assert restored == original
        assert restored.ingredients.items.value is None
        assert restored.ingredients.items.not_found_reason is not None

    def test_label_facts_are_frozen(self) -> None:
        facts = _fully_populated_facts()
        with pytest.raises(ValidationError):
            facts.quantity = QuantityFacts(net_quantity=Fact.found("1 kg"))  # type: ignore[misc]


class TestSchemaVersioning:
    def test_default_schema_version_matches_the_module_constant(self) -> None:
        facts = _fully_populated_facts()
        assert facts.schema_version == SCHEMA_VERSION == "1.0.0"

    def test_an_incompatible_schema_version_is_rejected(self) -> None:
        payload = _fully_populated_facts().model_dump()
        payload["schema_version"] = "2.0.0"
        with pytest.raises(ValidationError):
            LabelFacts.model_validate(payload)

    def test_json_schema_documents_every_fact_category(self) -> None:
        schema = LabelFacts.model_json_schema()
        top_level = set(schema["properties"].keys())
        assert top_level == {
            "schema_version",
            "ingredients",
            "allergens",
            "nutrition",
            "quantity",
            "dates",
            "claims",
            "addresses",
            "languages",
        }


class TestFieldLevelValidation:
    def test_address_rejects_an_unknown_role(self) -> None:
        with pytest.raises(ValidationError):
            Address(role="wholesaler", text="somewhere")  # type: ignore[arg-type]

    def test_ingredient_item_percentage_is_optional(self) -> None:
        item = IngredientItem(name="Water", position=0)
        assert item.percentage is None


def test_module_exports_a_single_source_of_truth_for_schema_version() -> None:
    # Guards against the constant and the field default silently drifting apart.
    assert facts_module.SCHEMA_VERSION == LabelFacts.model_fields["schema_version"].default
