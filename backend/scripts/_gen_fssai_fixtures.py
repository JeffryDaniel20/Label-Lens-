"""One-off generator for app/rulesets/in-fssai-food/v1.0.0/fixtures/*.

Not part of the runtime or test suite - run once to produce the fixture
JSON files below, then the files themselves (not this script) are what
`tests/rules/test_in_fssai_food_pack.py` actually loads and asserts against.
Kept in `scripts/` for reproducibility (regenerate if a fixture needs to
change) rather than hand-editing 20+ JSON files directly.

Shares its "fully-compliant label" baseline with P4-T6's `new_rule.py` via
`_fixture_baseline.py` - see that module's own docstring for why there is
deliberately only one such baseline in this codebase.
"""

from __future__ import annotations

import json
from pathlib import Path

from _fixture_baseline import BASE_COMPLIANT_FACTS as BASE
from _fixture_baseline import fact, with_field

from app.extraction.facts import LabelFacts

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "app/rulesets/in-fssai-food/v1.0.0/fixtures"

# rule_key -> (pass_facts, fail_facts | None, insufficient_facts)
CASES: dict[str, tuple[dict[str, object], dict[str, object] | None, dict[str, object]]] = {
    "IN-FSSAI-FOOD-INGREDIENTS-LIST-DECLARED": (
        BASE,
        with_field(BASE, "ingredients.declared_text", fact("   ")),
        with_field(BASE, "ingredients.declared_text", fact(None, "no ingredient list printed")),
    ),
    "IN-FSSAI-FOOD-INGREDIENTS-ITEMS-ITEMIZED": (
        BASE,
        None,
        with_field(BASE, "ingredients.items", fact(None, "ingredient list illegible")),
    ),
    "IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED": (
        BASE,
        with_field(BASE, "allergens.declared", fact(["Wheat", "Unobtainium"])),
        with_field(BASE, "allergens.declared", fact(None, "allergen statement illegible")),
    ),
    "IN-FSSAI-FOOD-NET-QUANTITY-DECLARED": (
        BASE,
        with_field(BASE, "quantity.net_quantity", fact("  ")),
        with_field(BASE, "quantity.net_quantity", fact(None, "net quantity not printed")),
    ),
    "IN-FSSAI-FOOD-MANUFACTURE-DATE-DECLARED": (
        BASE,
        with_field(BASE, "dates.manufacture_date", fact("  ")),
        with_field(BASE, "dates.manufacture_date", fact(None, "manufacture date not printed")),
    ),
    "IN-FSSAI-FOOD-EXPIRY-DATE-DECLARED": (
        BASE,
        with_field(BASE, "dates.expiry_or_best_before", fact("  ")),
        with_field(BASE, "dates.expiry_or_best_before", fact(None, "expiry date not printed")),
    ),
    "IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED": (
        BASE,
        with_field(BASE, "dates.batch_number", fact("  ")),
        with_field(BASE, "dates.batch_number", fact(None, "batch number not printed")),
    ),
    "IN-FSSAI-FOOD-BRAND-OWNER-ADDRESS-DECLARED": (
        BASE,
        None,
        with_field(BASE, "addresses.items", fact(None, "no address block found")),
    ),
    "IN-FSSAI-FOOD-NUTRITION-INFO-DECLARED": (
        BASE,
        None,
        with_field(BASE, "nutrition.rows", fact(None, "nutrition table not printed")),
    ),
    "IN-FSSAI-FOOD-NUTRITION-SERVING-SIZE-DECLARED": (
        BASE,
        with_field(BASE, "nutrition.serving_size", fact("  ")),
        with_field(BASE, "nutrition.serving_size", fact(None, "serving size not printed")),
    ),
    "IN-FSSAI-FOOD-LABEL-LANGUAGE-COMPLIANT": (
        BASE,
        with_field(BASE, "languages.detected", fact(["ta"])),
        with_field(BASE, "languages.detected", fact(None, "no legible text detected")),
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
