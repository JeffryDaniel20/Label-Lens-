"""Table-driven tests for locale decimal number parsing (P3-T7)."""

from __future__ import annotations

import pytest

from app.extraction.normalize.numbers import NumberParseError, parse_locale_number

pytestmark = pytest.mark.unit


class TestLocaleNumbers:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12.5", 12.5),
            ("0.5", 0.5),
            ("250", 250.0),
            ("12,5", 12.5),  # European decimal comma
            ("0,5", 0.5),
            ("1,234", 1234.0),  # thousands separator, not a decimal
            ("12,345", 12345.0),  # 3+ trailing digits -> thousands, not decimal
            ("1,234.56", 1234.56),  # US: comma thousands, dot decimal
            ("1.234,56", 1234.56),  # EU: dot thousands, comma decimal
            ("1.234.567", 1234567.0),  # multiple dots -> thousands separators
            ("-3.5", -3.5),
            ("-3,5", -3.5),
            ("+2.5", 2.5),
            (" 12.5 ", 12.5),
            ("1 234,5", 1234.5),  # space as a thousands separator
        ],
    )
    def test_parses_expected_value(self, text: str, expected: float) -> None:
        assert parse_locale_number(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["", "   ", "abc", "%", "12%"])
    def test_rejects_unparsable_input(self, text: str) -> None:
        with pytest.raises(NumberParseError):
            parse_locale_number(text)
