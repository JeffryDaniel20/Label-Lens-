"""Ingredient-statement parsing.

Splits a declared ingredient statement into its top-level ingredients. A
naive comma-split would incorrectly break apart a compound ingredient's own
parenthesised sub-ingredient list - e.g. "Chocolate (Sugar, Cocoa Butter),
Wheat Flour" must become `["Chocolate (Sugar, Cocoa Butter)", "Wheat
Flour"]`, not four fragments - so commas are only treated as separators
outside any parentheses, at any nesting depth. A trailing QUID percentage
declaration is extracted only when the *entire* parenthetical is nothing but
a percentage (e.g. "Salt (1.5%)"); a sub-ingredient list's own parenthetical
(no bare percentage) is left as part of the ingredient's name untouched.
"""

from __future__ import annotations

import re

from app.extraction.facts import IngredientItem
from app.extraction.normalize.numbers import NumberParseError, parse_locale_number

_PERCENTAGE_SUFFIX = re.compile(r"^(?P<name>.*?)\s*\(\s*(?P<pct>[\d.,]+)\s*%\s*\)$")


def split_top_level(text: str, separator: str = ",") -> list[str]:
    """Split `text` on `separator`, ignoring separators inside parentheses."""
    segments: list[str] = []
    current: list[str] = []
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
            current.append(char)
        elif char == ")":
            depth = max(0, depth - 1)
            current.append(char)
        elif char == separator and depth == 0:
            segments.append("".join(current))
            current = []
        else:
            current.append(char)
    segments.append("".join(current))
    return [segment.strip() for segment in segments if segment.strip()]


def parse_ingredients(declared_text: str) -> list[IngredientItem]:
    """Parse a declared ingredient statement into ordered `IngredientItem`s."""
    items: list[IngredientItem] = []
    for position, segment in enumerate(split_top_level(declared_text)):
        name, percentage = segment, None
        if match := _PERCENTAGE_SUFFIX.match(segment):
            try:
                percentage = parse_locale_number(match.group("pct"))
                name = match.group("name").strip()
            except NumberParseError:
                name, percentage = segment, None
        items.append(IngredientItem(name=name, position=position, percentage=percentage))
    return items
