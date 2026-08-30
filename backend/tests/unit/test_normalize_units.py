"""Table-driven tests for unit normalization and nutrition-basis detection
(P3-T7). Covers the required "per-100g vs per-serving" acceptance case."""

from __future__ import annotations

import pytest

from app.extraction.normalize.units import (
    Quantity,
    UnitParseError,
    parse_nutrition_basis,
    parse_quantity,
)

pytestmark = pytest.mark.unit


class TestMassAndVolume:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("250 g", Quantity(250.0, "g")),
            ("250g", Quantity(250.0, "g")),
            ("0.5 kg", Quantity(500.0, "g")),
            ("500 mg", Quantity(0.5, "g")),
            ("12,5 g", Quantity(12.5, "g")),  # locale decimal comma
            ("100 mcg", Quantity(0.0001, "g")),
            ("100 µg", Quantity(0.0001, "g")),
            ("1.5 L", Quantity(1500.0, "ml")),
            ("1.5 l", Quantity(1500.0, "ml")),
            ("330 ml", Quantity(330.0, "ml")),
            ("5 cl", Quantity(50.0, "ml")),
        ],
    )
    def test_converts_to_the_canonical_unit(self, text: str, expected: Quantity) -> None:
        result = parse_quantity(text)
        assert result.unit == expected.unit
        assert result.value == pytest.approx(expected.value)

    def test_iu_is_recognized_but_never_converted(self) -> None:
        result = parse_quantity("400 IU")
        assert result == Quantity(400.0, "IU")

    @pytest.mark.parametrize("text", ["3 furlongs", "not a quantity", "g", "5"])
    def test_rejects_unrecognized_input(self, text: str) -> None:
        with pytest.raises(UnitParseError):
            parse_quantity(text)


class TestNutritionBasis:
    @pytest.mark.parametrize(
        ("header", "expected"),
        [
            ("Per 100g", "per_100"),
            ("per 100 g", "per_100"),
            ("PER 100ML", "per_100"),
            ("/100g", "per_100"),
            ("per serving", "per_serving"),
            ("Per Serving (30g)", "per_serving"),
            ("each serving provides", "per_serving"),
            ("per portion", "per_serving"),
            ("Nutritional Information", "unknown"),
            ("Energy", "unknown"),
        ],
    )
    def test_classifies_the_column_basis(self, header: str, expected: str) -> None:
        assert parse_nutrition_basis(header) == expected
