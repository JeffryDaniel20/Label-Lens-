"""One-off generator for app/rulesets/in-fssai-food/v1.0.0/fixtures/*.

Not part of the runtime or test suite - run once to produce the fixture
JSON files below, then the files themselves (not this script) are what
`tests/rules/test_in_fssai_food_pack.py` actually loads and asserts against.
Kept in `scripts/` for reproducibility (regenerate if a fixture needs to
change) rather than hand-editing 20+ JSON files directly.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from app.extraction.facts import LabelFacts

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "app/rulesets/in-fssai-food/v1.0.0/fixtures"


def _fact(value: object, reason: str | None = None) -> dict[str, object]:
    if reason is not None:
        return {"value": None, "not_found_reason": reason}
    return {"value": value, "not_found_reason": None}


BASE: dict[str, object] = {
    "schema_version": "1.0.0",
    "ingredients": {
        "declared_text": _fact("Sugar, Wheat Flour, Milk Solids, Cocoa Solids"),
        "items": _fact(
            [
                {"name": "Sugar", "position": 0, "percentage": None},
                {"name": "Wheat Flour", "position": 1, "percentage": None},
                {"name": "Milk Solids", "position": 2, "percentage": None},
                {"name": "Cocoa Solids", "position": 3, "percentage": None},
            ]
        ),
    },
    "allergens": {
        "declaration_text": _fact("Contains: Wheat, Milk"),
        "declared": _fact(["Wheat", "Milk"]),
    },
    "nutrition": {
        "serving_size": _fact("30 g"),
        "rows": _fact(
            [
                {"nutrient": "Energy", "unit": "kcal", "per_100g": 450.0, "per_serving": 135.0},
                {"nutrient": "Protein", "unit": "g", "per_100g": 6.0, "per_serving": 1.8},
            ]
        ),
    },
    "quantity": {"net_quantity": _fact("100 g")},
    "dates": {
        "manufacture_date": _fact("01/2026"),
        "expiry_or_best_before": _fact("01/2027"),
        "batch_number": _fact("B12345"),
    },
    "claims": {"items": _fact([])},
    "addresses": {
        "items": _fact(
            [
                {
                    "role": "manufacturer",
                    "text": "ABC Foods Pvt Ltd, MIDC, Pune, Maharashtra 411019, India",
                }
            ]
        )
    },
    "languages": {"detected": _fact(["en"])},
}


def _set(
    facts: dict[str, object], section: str, field: str, value: dict[str, object]
) -> dict[str, object]:
    out = copy.deepcopy(facts)
    out[section][field] = value  # type: ignore[index]
    return out


# rule_key -> (pass_facts, fail_facts | None, insufficient_facts)
CASES: dict[str, tuple[dict[str, object], dict[str, object] | None, dict[str, object]]] = {
    "IN-FSSAI-FOOD-INGREDIENTS-LIST-DECLARED": (
        BASE,
        _set(BASE, "ingredients", "declared_text", _fact("   ")),
        _set(BASE, "ingredients", "declared_text", _fact(None, "no ingredient list printed")),
    ),
    "IN-FSSAI-FOOD-INGREDIENTS-ITEMS-ITEMIZED": (
        BASE,
        None,
        _set(BASE, "ingredients", "items", _fact(None, "ingredient list illegible")),
    ),
    "IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED": (
        BASE,
        _set(BASE, "allergens", "declared", _fact(["Wheat", "Unobtainium"])),
        _set(BASE, "allergens", "declared", _fact(None, "allergen statement illegible")),
    ),
    "IN-FSSAI-FOOD-NET-QUANTITY-DECLARED": (
        BASE,
        _set(BASE, "quantity", "net_quantity", _fact("  ")),
        _set(BASE, "quantity", "net_quantity", _fact(None, "net quantity not printed")),
    ),
    "IN-FSSAI-FOOD-MANUFACTURE-DATE-DECLARED": (
        BASE,
        _set(BASE, "dates", "manufacture_date", _fact("  ")),
        _set(BASE, "dates", "manufacture_date", _fact(None, "manufacture date not printed")),
    ),
    "IN-FSSAI-FOOD-EXPIRY-DATE-DECLARED": (
        BASE,
        _set(BASE, "dates", "expiry_or_best_before", _fact("  ")),
        _set(BASE, "dates", "expiry_or_best_before", _fact(None, "expiry date not printed")),
    ),
    "IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED": (
        BASE,
        _set(BASE, "dates", "batch_number", _fact("  ")),
        _set(BASE, "dates", "batch_number", _fact(None, "batch number not printed")),
    ),
    "IN-FSSAI-FOOD-BRAND-OWNER-ADDRESS-DECLARED": (
        BASE,
        None,
        _set(BASE, "addresses", "items", _fact(None, "no address block found")),
    ),
    "IN-FSSAI-FOOD-NUTRITION-INFO-DECLARED": (
        BASE,
        None,
        _set(BASE, "nutrition", "rows", _fact(None, "nutrition table not printed")),
    ),
    "IN-FSSAI-FOOD-NUTRITION-SERVING-SIZE-DECLARED": (
        BASE,
        _set(BASE, "nutrition", "serving_size", _fact("  ")),
        _set(BASE, "nutrition", "serving_size", _fact(None, "serving size not printed")),
    ),
    "IN-FSSAI-FOOD-LABEL-LANGUAGE-COMPLIANT": (
        BASE,
        _set(BASE, "languages", "detected", _fact(["ta"])),
        _set(BASE, "languages", "detected", _fact(None, "no legible text detected")),
    ),
}


def main() -> None:
    for rule_key, (pass_facts, fail_facts, insufficient_facts) in CASES.items():
        out_dir = FIXTURES_DIR / rule_key
        out_dir.mkdir(parents=True, exist_ok=True)
        cases = {"pass": pass_facts, "insufficient_data": insufficient_facts}
        if fail_facts is not None:
            cases["fail"] = fail_facts
        for name, facts in cases.items():
            validated = LabelFacts.model_validate(facts).model_dump(mode="json")
            (out_dir / f"{name}.json").write_text(
                json.dumps(validated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
    print(f"Wrote fixtures for {len(CASES)} rules to {FIXTURES_DIR}")


if __name__ == "__main__":
    main()
