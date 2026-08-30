"""Tests that the normalization library exposes a stable, importable
version, and that its functions produce values usable directly as
`app.extraction.facts` field values (P3-T7 depends on P3-T4)."""

from __future__ import annotations

import pytest

from app.extraction.facts import Fact, IngredientsFacts, NutritionFacts, NutritionRow
from app.extraction.normalize import NORMALIZER_VERSION
from app.extraction.normalize.dates import normalize_label_date
from app.extraction.normalize.ingredients import parse_ingredients
from app.extraction.normalize.units import parse_nutrition_basis, parse_quantity

pytestmark = pytest.mark.unit


def test_normalizer_version_is_a_stable_string() -> None:
    assert isinstance(NORMALIZER_VERSION, str)
    assert NORMALIZER_VERSION == "1.0.0"


def test_normalized_ingredients_populate_a_real_ingredients_fact() -> None:
    items = parse_ingredients("Wheat Flour, Sugar, Salt (1.5%)")
    facts = IngredientsFacts(
        declared_text=Fact.found("Wheat Flour, Sugar, Salt (1.5%)"),
        items=Fact.found(items),
    )
    assert facts.items.value is not None
    assert facts.items.value[2].percentage == pytest.approx(1.5)


def test_normalized_quantity_and_date_are_usable_downstream() -> None:
    quantity = parse_quantity("250 g")
    expiry = normalize_label_date("DEC 2027")
    basis = parse_nutrition_basis("per 100g")

    nutrition = NutritionFacts(
        serving_size=Fact.found("30 g"),
        rows=Fact.found(
            [NutritionRow(nutrient="energy", unit="kcal", per_100g=380.0)]
            if basis == "per_100"
            else []
        ),
    )
    assert quantity.value == pytest.approx(250.0)
    assert quantity.unit == "g"
    assert expiry == "2027-12"
    assert nutrition.rows.value is not None
