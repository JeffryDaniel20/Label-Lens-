"""Tests for deterministic per-block language detection (P3-T7)."""

from __future__ import annotations

import pytest

from app.extraction.normalize.language import detect_language

pytestmark = pytest.mark.unit


class TestLanguageDetection:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("Ingredients: Wheat Flour, Sugar, Salt", "en"),
            ("Contains: Milk, Soy, and Wheat allergens", "en"),
            ("Zutaten: Zucker, Salz, Milchpulver", "de"),
            ("Ingrédients : sucre, sel, farine de blé", "fr"),
        ],
    )
    def test_detects_the_expected_language_on_a_realistic_block(
        self, text: str, expected: str
    ) -> None:
        assert detect_language(text) == expected

    @pytest.mark.parametrize("text", ["", "  ", "Milk", "Contains"])
    def test_short_text_returns_none_rather_than_a_guess(self, text: str) -> None:
        assert detect_language(text) is None

    def test_detection_is_deterministic_across_repeated_calls(self) -> None:
        text = "Ingredients: Wheat Flour, Sugar, Salt, Emulsifier"
        results = {detect_language(text) for _ in range(20)}
        assert len(results) == 1
