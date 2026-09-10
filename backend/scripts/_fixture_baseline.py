"""The one, shared "fully-compliant label" `LabelFacts` baseline every rule
pack's generated fixtures are built from (P4-T5's `_gen_fssai_fixtures.py`
and P4-T6's `new_rule.py` both import this - neither defines its own copy).

Keeping exactly one baseline is a deliberate anti-drift choice, the same
reasoning `app.rules.predicates`'s own docstring gives for not duplicating
the allergen dictionary: two "what does a compliant label look like"
fixtures that could silently diverge would be a real correctness hazard in
a compliance product, not just untidy duplication.
"""

from __future__ import annotations

import copy

_FactDict = dict[str, object]


def fact(value: object, reason: str | None = None) -> _FactDict:
    """A `Fact`-shaped dict (`app.extraction.facts.Fact`'s own wire shape) -
    exactly one of `value`/`reason` given, matching that model's own
    "never both, never neither" invariant."""
    if reason is not None:
        return {"value": None, "not_found_reason": reason}
    return {"value": value, "not_found_reason": None}


# A hypothetical, fully-compliant packaged-food label - every field a rule
# in `in-fssai-food` might check is present, well-formed, and unambiguous.
BASE_COMPLIANT_FACTS: _FactDict = {
    "schema_version": "1.0.0",
    "ingredients": {
        "declared_text": fact("Sugar, Wheat Flour, Milk Solids, Cocoa Solids"),
        "items": fact(
            [
                {"name": "Sugar", "position": 0, "percentage": None},
                {"name": "Wheat Flour", "position": 1, "percentage": None},
                {"name": "Milk Solids", "position": 2, "percentage": None},
                {"name": "Cocoa Solids", "position": 3, "percentage": None},
            ]
        ),
    },
    "allergens": {
        "declaration_text": fact("Contains: Wheat, Milk"),
        "declared": fact(["Wheat", "Milk"]),
    },
    "nutrition": {
        "serving_size": fact("30 g"),
        "rows": fact(
            [
                {"nutrient": "Energy", "unit": "kcal", "per_100g": 450.0, "per_serving": 135.0},
                {"nutrient": "Protein", "unit": "g", "per_100g": 6.0, "per_serving": 1.8},
            ]
        ),
    },
    "quantity": {"net_quantity": fact("100 g")},
    "dates": {
        "manufacture_date": fact("01/2026"),
        "expiry_or_best_before": fact("01/2027"),
        "batch_number": fact("B12345"),
    },
    "claims": {"items": fact([])},
    "addresses": {
        "items": fact(
            [
                {
                    "role": "manufacturer",
                    "text": "ABC Foods Pvt Ltd, MIDC, Pune, Maharashtra 411019, India",
                }
            ]
        )
    },
    "languages": {"detected": fact(["en"])},
}

# The type each `LabelFacts` field path resolves to - "text" (a single
# `Fact[str]`, checkable with a blank/non-blank `fail` case) or "list" (a
# `Fact[list[...]]`, where only `pass`/`insufficient_data` are reachable
# through `field_present` alone - see `docs/rules-authoring.md`).
FIELD_KIND: dict[str, str] = {
    "ingredients.declared_text": "text",
    "ingredients.items": "list",
    "allergens.declaration_text": "text",
    "allergens.declared": "list",
    "nutrition.serving_size": "text",
    "nutrition.rows": "list",
    "quantity.net_quantity": "text",
    "dates.manufacture_date": "text",
    "dates.expiry_or_best_before": "text",
    "dates.batch_number": "text",
    "addresses.items": "list",
    "languages.detected": "list",
}


def with_field(facts: _FactDict, field_path: str, value: _FactDict) -> _FactDict:
    """A deep copy of `facts` with the dotted `field_path` (e.g.
    `"dates.batch_number"`) replaced by `value` - a `Fact`-shaped dict from
    `fact()`."""
    section, field = field_path.split(".", 1)
    out = copy.deepcopy(facts)
    out[section][field] = value  # type: ignore[index]
    return out
