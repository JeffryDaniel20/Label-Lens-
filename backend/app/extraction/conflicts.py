"""Conflicting-citation detection (P7-T4, IMPLEMENTATION.md section 23's
"Conflict" adversarial family).

`app/confidence/tiers.py`'s own routing table has always promised that
"conflicting duplicates" force mandatory review (section 14's table, copied
verbatim into that module's docstring) - but until this module existed
nothing ever detected one, so the promise was documentation only. This
closes that specific gap.

WHAT IS DETECTED, AND WHY ONLY THIS
-----------------------------------
A field whose *own cited OCR tokens disagree with each other* under the
field's real normalizer. The P3-T6 evidence gate cannot catch this: it
joins every cited token into one string and fuzzy-matches the asserted
value against that join, so a field asserting "250 g" while citing both
"250 g" and "500 g" matches the first window at ratio 1.0 and verifies
cleanly. The label genuinely shows two different values for one field and
nothing downstream would ever know.

WHAT IS DELIBERATELY NOT DETECTED, STATED PLAINLY
-------------------------------------------------
Section 23's example "two different net weights" *printed anywhere on the
label* - as opposed to both being cited for the same field - is NOT
detected here, and no heuristic pretending to is shipped. Deciding that
some other quantity-shaped token elsewhere on the packet is a second *net
quantity declaration* (rather than a serving size, a nutrition row, or a
pack count) needs label-region semantics this system does not have: every
compliant label in this codebase's own fixtures prints both a net quantity
("250 g") and a serving size ("30 g"), so a naive "any other parseable
quantity differs" scan would flag every correct label as conflicting. A
detector with that false-positive rate would be worse than none, because
it would train reviewers to ignore the signal. Catching the printed-twice
case properly belongs either to a rule (the rule engine already owns every
compliance judgement) or to a richer extraction schema that can report
more than one candidate per field - both genuinely larger than this task.

Only fields with a real, existing normalizer are checked: a canonical
comparison is the only way to tell "500 g" and "0.5 kg" (the same value,
not a conflict) from "500 g" and "250 g" (a real one).
"""

from __future__ import annotations

from collections.abc import Sequence

from app.extraction.normalize.dates import DateParseError, normalize_label_date
from app.extraction.normalize.units import UnitParseError, parse_quantity

# Only field paths whose values a real normalizer can canonicalize. Anything
# else (free text, lists) has no meaningful "same value, different spelling"
# comparison, so it is not guessed at.
_QUANTITY_FIELDS = frozenset({"quantity.net_quantity", "nutrition.serving_size"})
_DATE_FIELDS = frozenset(
    {"dates.manufacture_date", "dates.expiry_or_best_before"}
)


def _canonical_quantity(text: str) -> str | None:
    try:
        quantity = parse_quantity(text)
    except (UnitParseError, ValueError):
        return None
    return f"{quantity.value:g}{quantity.unit}"


def _canonical_date(text: str) -> str | None:
    try:
        return normalize_label_date(text)
    except (DateParseError, ValueError):
        return None


def canonicalize(field_path: str, text: str) -> str | None:
    """The canonical form of `text` for `field_path`, or `None` when this
    field has no normalizer or the text does not parse as one of its
    values. `None` always means "no opinion" - never "conflict"."""
    if field_path in _QUANTITY_FIELDS:
        return _canonical_quantity(text)
    if field_path in _DATE_FIELDS:
        return _canonical_date(text)
    return None


def find_conflicting_citation(field_path: str, cited_texts: Sequence[str]) -> str | None:
    """A human-readable reason when this field's own cited tokens carry two
    genuinely different canonical values, else `None`.

    Pure: no DB, no I/O. Unparseable citations are ignored rather than
    treated as conflicts - a token this field's normalizer has no opinion
    about is not evidence of disagreement.
    """
    canonical_values: dict[str, str] = {}
    for text in cited_texts:
        canonical = canonicalize(field_path, text)
        if canonical is None:
            continue
        canonical_values.setdefault(canonical, text)

    if len(canonical_values) < 2:
        return None

    shown = ", ".join(repr(raw) for raw in sorted(canonical_values.values()))
    return f"cited tokens disagree: {shown}"
