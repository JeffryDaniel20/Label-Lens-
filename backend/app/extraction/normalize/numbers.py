"""Locale-aware decimal number parsing.

Product labels mix US/ISO-style decimals ("12.5") with European-style
decimal commas ("12,5"), sometimes alongside a thousands separator of the
other kind ("1.234,56" or "1,234.56"). Parsing is pure string manipulation -
no dependency on the host's OS locale - so the same input always parses the
same way regardless of where the process runs.
"""

from __future__ import annotations

import re

_NUMERIC_CHARS = re.compile(r"^[+-]?[\d.,\s ]+$")


class NumberParseError(ValueError):
    pass


def parse_locale_number(text: str) -> float:
    """Parse `text` as a number, resolving `.`/`,` as decimal point or
    thousands separator using the following deterministic rules:

    - Both `.` and `,` present: whichever comes *last* is the decimal point
      (handles both "1,234.56" and "1.234,56").
    - Only `,` present: a single comma followed by exactly 1-2 digits reads
      as a decimal comma ("12,5" -> 12.5); anything else (no trailing digits
      matching that shape, or more than one comma) is a thousands separator
      ("1,234" -> 1234).
    - Only `.` present: always the decimal point, *unless* there is more
      than one (multiple dots can only be thousands separators, e.g.
      "1.234.567" -> 1234567).
    """
    raw = text.strip()
    if not raw:
        raise NumberParseError("Empty value.")

    negative = raw.startswith("-")
    compact = raw.lstrip("+-")
    if not _NUMERIC_CHARS.match(raw):
        raise NumberParseError(f"Not a recognizable number: {text!r}")
    compact = compact.replace(" ", "").replace(" ", "")

    has_dot = "." in compact
    has_comma = "," in compact
    if has_dot and has_comma:
        if compact.rfind(",") > compact.rfind("."):
            compact = compact.replace(".", "").replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif has_comma:
        parts = compact.split(",")
        if len(parts) == 2 and 1 <= len(parts[1]) <= 2:
            compact = compact.replace(",", ".")
        else:
            compact = compact.replace(",", "")
    elif has_dot and compact.count(".") > 1:
        compact = compact.replace(".", "")

    try:
        value = float(compact)
    except ValueError as exc:
        raise NumberParseError(f"Not a recognizable number: {text!r}") from exc
    return -value if negative else value
