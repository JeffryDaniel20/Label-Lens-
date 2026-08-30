"""Table-driven tests for the allergen synonym dictionary (P3-T7)."""

from __future__ import annotations

import pytest

from app.extraction.normalize.allergens import (
    ALLERGEN_SYNONYMS,
    CANONICAL_ALLERGENS,
    canonicalize_allergen,
    is_allergen,
)

pytestmark = pytest.mark.unit


class TestCanonicalization:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Milk", "milk"),
            ("dairy", "milk"),
            ("MILK POWDER", "milk"),
            ("Soya", "soybeans"),
            ("soy", "soybeans"),
            ("Wheat Flour", "wheat"),
            ("maida", "wheat"),
            ("Cashews", "tree_nuts"),
            ("almond", "tree_nuts"),
            ("Peanuts", "peanuts"),
            ("groundnut", "peanuts"),
            ("Prawns", "crustaceans"),
            ("Mussels", "molluscs"),
            ("Sesame Seeds", "sesame"),
            ("til", "sesame"),
            ("Sulfites", "sulphites"),
            ("  eggs  ", "eggs"),
        ],
    )
    def test_recognized_synonyms_canonicalize(self, text: str, expected: str) -> None:
        assert canonicalize_allergen(text) == expected
        assert is_allergen(text) is True

    @pytest.mark.parametrize("text", ["water", "sugar", "salt", "", "rice"])
    def test_non_allergens_are_not_recognized(self, text: str) -> None:
        assert canonicalize_allergen(text) is None
        assert is_allergen(text) is False


class TestDictionaryIntegrity:
    def test_every_synonym_maps_to_a_canonical_allergen(self) -> None:
        for synonym, canonical in ALLERGEN_SYNONYMS.items():
            assert canonical in CANONICAL_ALLERGENS, f"{synonym!r} -> unknown {canonical!r}"

    def test_every_canonical_allergen_has_at_least_one_synonym(self) -> None:
        covered = set(ALLERGEN_SYNONYMS.values())
        assert covered == set(CANONICAL_ALLERGENS)
