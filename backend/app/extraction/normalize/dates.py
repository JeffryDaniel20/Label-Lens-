"""Deterministic date normalization for label date declarations.

FSSAI (India) and EU labels are overwhelmingly day-first
(DD/MM/YYYY, DD.MM.YYYY, "DD Mon YYYY"), unlike the US's month-first
convention, so any purely-numeric slash/dot-separated date is parsed
day-first here. That is documented, not silently assumed, and matches this
project's two named target jurisdictions.

Many "best before" declarations print only a month and year ("DEC 2027",
"12/2027") rather than a full date - that produces a partial `"YYYY-MM"`
ISO result instead of raising, since a missing day is a real, common, valid
label convention, not a parse failure.
"""

from __future__ import annotations

import datetime as dt
import re

_MONTHS: dict[str, int] = {
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "may": 5,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

_ISO_FULL = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_ISO_MONTH = re.compile(r"^(\d{4})-(\d{1,2})$")
_NUMERIC_FULL = re.compile(r"^(\d{1,2})[./](\d{1,2})[./](\d{2,4})$")
_NUMERIC_MONTH_YEAR = re.compile(r"^(\d{1,2})[./](\d{4})$")
_DAY_MONTHNAME_YEAR = re.compile(r"^(\d{1,2})\s+([A-Za-z]{3,9})\.?,?\s+(\d{4})$")
_MONTHNAME_YEAR = re.compile(r"^([A-Za-z]{3,9})\.?\s+(\d{4})$")


class DateParseError(ValueError):
    pass


def _full_year(year: int) -> int:
    return year if year >= 100 else 2000 + year


def _month_number(name: str) -> int:
    key = name.lower().rstrip(".")[:3]
    month = _MONTHS.get(key)
    if month is None:
        raise DateParseError(f"Unrecognized month name: {name!r}")
    return month


def _date(year: int, month: int, day: int) -> dt.date:
    try:
        return dt.date(year, month, day)
    except ValueError as exc:
        raise DateParseError(f"Invalid date: year={year} month={month} day={day}") from exc


def normalize_label_date(text: str) -> str:
    """Return an ISO `YYYY-MM-DD` date, or `YYYY-MM` for a month/year-only
    declaration. Raises `DateParseError` for anything else."""
    raw = text.strip()

    if m := _ISO_FULL.match(raw):
        year, month, day = (int(g) for g in m.groups())
        return _date(year, month, day).isoformat()

    if m := _ISO_MONTH.match(raw):
        year, month = (int(g) for g in m.groups())
        _date(year, month, 1)  # validates the month
        return f"{year:04d}-{month:02d}"

    if m := _NUMERIC_FULL.match(raw):
        day_s, month_s, year_s = m.groups()
        year = _full_year(int(year_s))
        return _date(year, int(month_s), int(day_s)).isoformat()

    if m := _NUMERIC_MONTH_YEAR.match(raw):
        month_s, year_s = m.groups()
        month, year = int(month_s), int(year_s)
        _date(year, month, 1)
        return f"{year:04d}-{month:02d}"

    if m := _DAY_MONTHNAME_YEAR.match(raw):
        day_s, month_name, year_s = m.groups()
        month = _month_number(month_name)
        return _date(int(year_s), month, int(day_s)).isoformat()

    if m := _MONTHNAME_YEAR.match(raw):
        month_name, year_s = m.groups()
        month = _month_number(month_name)
        return f"{int(year_s):04d}-{month:02d}"

    raise DateParseError(f"Could not parse a date from {text!r}")
