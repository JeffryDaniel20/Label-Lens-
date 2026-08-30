"""Table-driven tests for ingredient-statement parsing (P3-T7).

Covers the required "nested parentheses" acceptance case.
"""

from __future__ import annotations

import pytest

from app.extraction.normalize.ingredients import parse_ingredients, split_top_level

pytestmark = pytest.mark.unit


class TestSplitTopLevel:
    def test_plain_comma_separated_list(self) -> None:
        assert split_top_level("Wheat Flour, Sugar, Salt") == [
            "Wheat Flour",
            "Sugar",
            "Salt",
        ]

    def test_does_not_split_inside_parentheses(self) -> None:
        text = "Chocolate (Sugar, Cocoa Butter, Milk Solids), Wheat Flour, Salt"
        assert split_top_level(text) == [
            "Chocolate (Sugar, Cocoa Butter, Milk Solids)",
            "Wheat Flour",
            "Salt",
        ]

    def test_handles_nested_parentheses(self) -> None:
        text = "Emulsifier (Soy Lecithin (E322), Mono- and Diglycerides), Salt"
        assert split_top_level(text) == [
            "Emulsifier (Soy Lecithin (E322), Mono- and Diglycerides)",
            "Salt",
        ]

    def test_unbalanced_closing_parenthesis_does_not_crash(self) -> None:
        # Depth is clamped at 0 rather than going negative on OCR noise.
        assert split_top_level("Sugar), Salt") == ["Sugar)", "Salt"]

    def test_empty_segments_are_dropped(self) -> None:
        assert split_top_level("Sugar,, Salt") == ["Sugar", "Salt"]


class TestParseIngredients:
    def test_simple_list_with_no_percentages(self) -> None:
        items = parse_ingredients("Wheat Flour, Sugar, Salt")
        assert [i.name for i in items] == ["Wheat Flour", "Sugar", "Salt"]
        assert [i.position for i in items] == [0, 1, 2]
        assert all(i.percentage is None for i in items)

    def test_a_bare_percentage_declaration_is_extracted(self) -> None:
        items = parse_ingredients("Wheat Flour, Salt (1.5%)")
        assert items[1].name == "Salt"
        assert items[1].percentage == pytest.approx(1.5)

    def test_a_locale_decimal_percentage_is_extracted(self) -> None:
        items = parse_ingredients("Salt (1,5%)")
        assert items[0].percentage == pytest.approx(1.5)

    def test_a_sub_ingredient_parenthetical_is_kept_as_part_of_the_name(self) -> None:
        # This is the key nested-parentheses case: "Chocolate (...)" has no
        # bare percentage, so its parenthetical must NOT be stripped, and the
        # commas inside it must NOT create extra top-level ingredients.
        items = parse_ingredients(
            "Chocolate (Sugar, Cocoa Butter, Milk Solids), Wheat Flour, Salt (1.5%)"
        )
        assert len(items) == 3
        assert items[0].name == "Chocolate (Sugar, Cocoa Butter, Milk Solids)"
        assert items[0].percentage is None
        assert items[1].name == "Wheat Flour"
        assert items[2].name == "Salt"
        assert items[2].percentage == pytest.approx(1.5)

    def test_doubly_nested_parentheses(self) -> None:
        items = parse_ingredients(
            "Emulsifier (Soy Lecithin (E322)), Salt (1%)"
        )
        assert items[0].name == "Emulsifier (Soy Lecithin (E322))"
        assert items[0].percentage is None
        assert items[1].percentage == pytest.approx(1.0)

    def test_empty_statement_yields_no_items(self) -> None:
        assert parse_ingredients("") == []
