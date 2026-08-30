"""Table-driven tests for label date normalization (P3-T7)."""

from __future__ import annotations

import pytest

from app.extraction.normalize.dates import DateParseError, normalize_label_date

pytestmark = pytest.mark.unit


class TestFullDates:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("25/12/2027", "2027-12-25"),  # day-first, matches FSSAI/EU convention
            ("25.12.2027", "2027-12-25"),
            ("01/02/2027", "2027-02-01"),  # NOT US month-first: this is 1 Feb, not 2 Jan
            ("25/12/27", "2027-12-25"),  # 2-digit year
            ("2027-12-25", "2027-12-25"),  # already ISO
            ("25 Dec 2027", "2027-12-25"),
            ("1 Jan 2026", "2026-01-01"),
            ("25 December 2027", "2027-12-25"),
        ],
    )
    def test_parses_full_dates_as_day_first(self, text: str, expected: str) -> None:
        assert normalize_label_date(text) == expected


class TestMonthYearOnly:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("12/2027", "2027-12"),
            ("12.2027", "2027-12"),
            ("DEC 2027", "2027-12"),
            ("December 2027", "2027-12"),
            ("2027-12", "2027-12"),
        ],
    )
    def test_parses_month_and_year_only_declarations(self, text: str, expected: str) -> None:
        """A "best before" printed without a day is a real, common label
        convention - it must normalize to a partial ISO date, not raise."""
        assert normalize_label_date(text) == expected


class TestRejectsUnparsable:
    @pytest.mark.parametrize(
        "text", ["not a date", "", "32/13/2027", "Frobtember 2027", "2027"]
    )
    def test_rejects_unparsable_or_invalid_dates(self, text: str) -> None:
        with pytest.raises(DateParseError):
            normalize_label_date(text)
