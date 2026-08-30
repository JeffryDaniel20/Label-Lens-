"""Deterministic unit normalization for mass/volume quantities and nutrition
table basis (per-100g vs per-serving) detection.

Converts recognized mass units to grams and volume units to millilitres, so
downstream rule logic can compare quantities without unit-awareness of its
own. "IU" (International Units) is recognized but deliberately never
converted: its ratio to any mass unit is substance-specific (1 IU of vitamin
D is not the same mass as 1 IU of vitamin A), so converting it without that
context would silently fabricate a number - it is passed through unchanged
instead, exactly like the fact schema treats an unreadable field as an
explicit absence rather than a guess.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from app.extraction.normalize.numbers import parse_locale_number

_MASS_TO_GRAMS: dict[str, float] = {
    "mg": 0.001,
    "milligram": 0.001,
    "milligrams": 0.001,
    "g": 1.0,
    "gram": 1.0,
    "grams": 1.0,
    "kg": 1000.0,
    "kilogram": 1000.0,
    "kilograms": 1000.0,
    "mcg": 1e-6,
    "µg": 1e-6,
    "ug": 1e-6,
    "microgram": 1e-6,
    "micrograms": 1e-6,
}

_VOLUME_TO_ML: dict[str, float] = {
    "ml": 1.0,
    "millilitre": 1.0,
    "milliliter": 1.0,
    "millilitres": 1.0,
    "milliliters": 1.0,
    "cl": 10.0,
    "l": 1000.0,
    "litre": 1000.0,
    "liter": 1000.0,
    "litres": 1000.0,
    "liters": 1000.0,
}

Dimension = Literal["mass", "volume", "iu"]


@dataclass(slots=True, frozen=True)
class Quantity:
    value: float
    unit: str  # canonical "g" or "ml"; "IU" (unconverted) otherwise


class UnitParseError(ValueError):
    pass


_QUANTITY_RE = re.compile(r"^\s*([+\-\d.,\s]+?)\s*([^\d\s]+)\s*$")


def parse_quantity(text: str) -> Quantity:
    match = _QUANTITY_RE.match(text)
    if not match:
        raise UnitParseError(f"Could not parse a quantity from {text!r}")
    number_part, unit_part = match.groups()
    value = parse_locale_number(number_part)
    unit_key = unit_part.strip().lower().rstrip(".")

    if unit_key == "iu":
        return Quantity(value=value, unit="IU")
    if unit_key in _MASS_TO_GRAMS:
        return Quantity(value=value * _MASS_TO_GRAMS[unit_key], unit="g")
    if unit_key in _VOLUME_TO_ML:
        return Quantity(value=value * _VOLUME_TO_ML[unit_key], unit="ml")
    raise UnitParseError(f"Unrecognized unit {unit_part!r} in {text!r}")


NutritionBasis = Literal["per_100", "per_serving", "unknown"]

_PER_100_RE = re.compile(r"per\s*100\s*(g|ml|gram|millilitre)|/\s*100\s*(g|ml)", re.IGNORECASE)
_PER_SERVING_RE = re.compile(r"per\s+serving|per\s+portion|each\s+serving", re.IGNORECASE)


def parse_nutrition_basis(header_text: str) -> NutritionBasis:
    """Classify a nutrition-table column header as per-100g/ml or per-serving."""
    if _PER_100_RE.search(header_text):
        return "per_100"
    if _PER_SERVING_RE.search(header_text):
        return "per_serving"
    return "unknown"
