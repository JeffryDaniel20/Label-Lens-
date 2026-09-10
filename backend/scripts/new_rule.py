"""P4-T6: a CLI that scaffolds a new rule (YAML) and its fixtures (JSON) into
an existing rule pack directory - so a new rule can be added, tested, and
published without touching Python. See `docs/rules-authoring.md` for the
full authoring workflow this CLI is one step of.

What it generates is deliberately the *common* case - a single field that
must be declared (and, for a text field, non-blank): most FSSAI-style "is X
declared on the label" rules are exactly this shape, covering 9 of the 11
rules in `in-fssai-food` v1.0.0 today. Anything more elaborate (a `for_each`
allergen check, an `any`/`all` combinator, a predicate needing
`predicate_kwargs`) still starts from the scaffolded file - only its
`logic:` block needs hand-editing afterward, which is YAML, never Python.

Usage:
    python scripts/new_rule.py <pack_dir> <RULE-KEY> \\
        --field dates.best_before_date \\
        --title "Best-before date is declared" \\
        --citation "Some Regulation, Clause X: ..." \\
        [--kind text|list] [--severity critical|major|minor|advisory] \\
        [--jurisdiction IN] [--category packaged_food]

The rule is validated against the real `app.rules.schema.Rule` model before
anything is written to disk, so a scaffolded rule can never be left in a
structurally broken state.

`--kind` defaults to a guess from `--field`'s last path segment (see
`_guess_kind`) when the field isn't already known to
`_fixture_baseline.FIELD_KIND`; pass it explicitly for a field this pack
has never checked before.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import yaml
from _fixture_baseline import BASE_COMPLIANT_FACTS, FIELD_KIND, fact, with_field

from app.extraction.facts import LabelFacts
from app.rules.schema import Rule, Severity

_RULE_KEY_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)+$")
_FIELD_PATH_RE = re.compile(r"^[a-z][a-z0-9_]*\.[a-z][a-z0-9_]*$")

_LIST_LEAVES = frozenset({"items", "declared", "rows"})


def _guess_kind(field: str) -> str:
    if field in FIELD_KIND:
        return FIELD_KIND[field]
    # A list-shaped `LabelFacts` field is always named `items`, `declared`,
    # or `rows` - every other leaf in the schema is a single `Fact[str]`.
    leaf = field.rsplit(".", 1)[-1]
    return "list" if leaf in _LIST_LEAVES else "text"


def _next_rule_filename(rules_dir: Path, rule_key: str) -> Path:
    existing = sorted(p.name for p in rules_dir.glob("*.yaml"))
    numbers = [int(m.group(1)) for name in existing if (m := re.match(r"^(\d+)_", name))]
    next_n = (max(numbers) + 1) if numbers else 1
    slug = rule_key.lower().replace("-", "_")
    return rules_dir / f"{next_n:02d}_{slug}.yaml"


def _build_rule(
    *,
    rule_key: str,
    title: str,
    citation: str,
    field: str,
    kind: str,
    severity: str,
    jurisdiction: str,
    category: str,
) -> dict[str, object]:
    if kind == "text":
        logic: dict[str, object] = {"regex_matches": {"path": field, "pattern": r"\S"}}
        fail_msg = "This declaration is present but blank on the label."
    else:
        logic = {"field_present": field}
        fail_msg = f"{title} could not be verified."
        citation = (
            citation
            + "\n\nNote: given only `field_present` is available to check a list-shaped "
            "fact, this rule's \"fail\" outcome is not currently reachable in practice - "
            "it can only ever resolve to `pass` or `insufficient_data`; this is stated "
            "plainly rather than papered over with a fabricated fail fixture."
        )

    return {
        "rule_key": rule_key,
        "version": 1,
        "title": title,
        "citation": citation,
        "severity": severity,
        "effective_from": "2022-07-01",
        "effective_to": None,
        "applicability": {
            "jurisdiction": [jurisdiction],
            "category": [category],
            "predicates": [],
        },
        "logic": logic,
        "requires_fields": [field],
        "on_missing_fields": "insufficient_data",
        "message": {
            "pass": f"{title}.",
            "fail": fail_msg,
            "insufficient_data": "Could not determine this from the extracted label data.",
        },
        "evidence": {"fields": [field]},
    }


def _write_fixtures(fixtures_dir: Path, rule_key: str, field: str, kind: str) -> list[Path]:
    out_dir = fixtures_dir / rule_key
    out_dir.mkdir(parents=True, exist_ok=True)
    cases = {
        "pass": BASE_COMPLIANT_FACTS,
        "insufficient_data": with_field(
            BASE_COMPLIANT_FACTS, field, fact(None, "not printed on this label")
        ),
    }
    if kind == "text":
        cases["fail"] = with_field(BASE_COMPLIANT_FACTS, field, fact("   "))
    written = []
    for name, facts in cases.items():
        validated = LabelFacts.model_validate(facts).model_dump(mode="json")
        path = out_dir / f"{name}.json"
        path.write_text(
            json.dumps(validated, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        written.append(path)
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Scaffold a new rule + fixtures into an existing rule pack (P4-T6)."
    )
    parser.add_argument("pack_dir", type=Path, help="e.g. app/rulesets/in-fssai-food/v1.0.0")
    parser.add_argument("rule_key", help="e.g. IN-FSSAI-FOOD-BEST-BEFORE-DATE-DECLARED")
    parser.add_argument(
        "--field", required=True, help="dotted LabelFacts path, e.g. dates.batch_number"
    )
    parser.add_argument("--title", required=True)
    parser.add_argument("--citation", required=True, help="the exact regulation clause quoted")
    parser.add_argument("--kind", choices=["text", "list"], default=None)
    parser.add_argument(
        "--severity", choices=[s.value for s in Severity], default=Severity.MAJOR.value
    )
    parser.add_argument("--jurisdiction", default="IN")
    parser.add_argument("--category", default="packaged_food")
    args = parser.parse_args(argv)

    if not _RULE_KEY_RE.match(args.rule_key):
        parser.error(f"rule_key {args.rule_key!r} must be upper-snake segments joined by '-'.")
    if not _FIELD_PATH_RE.match(args.field):
        parser.error(f"--field {args.field!r} must be a dotted lower_snake_case path.")

    pack_dir: Path = args.pack_dir
    rules_dir = pack_dir / "rules"
    fixtures_dir = pack_dir / "fixtures"
    if not rules_dir.is_dir():
        parser.error(f"{rules_dir} does not exist - is {pack_dir} a real pack directory?")

    rule_path = _next_rule_filename(rules_dir, args.rule_key)
    if rule_path.exists() or (fixtures_dir / args.rule_key).exists():
        parser.error(f"{args.rule_key} already appears to exist under {pack_dir}.")

    kind = args.kind or _guess_kind(args.field)
    rule_dict = _build_rule(
        rule_key=args.rule_key,
        title=args.title,
        citation=args.citation,
        field=args.field,
        kind=kind,
        severity=args.severity,
        jurisdiction=args.jurisdiction,
        category=args.category,
    )
    # Validate against the real schema before writing anything - a rule this
    # CLI produces can never be left on disk in a structurally broken state.
    Rule.model_validate(rule_dict)

    rule_path.write_text(
        yaml.safe_dump(rule_dict, sort_keys=False, default_flow_style=False, allow_unicode=True),
        encoding="utf-8",
    )
    fixture_paths = _write_fixtures(fixtures_dir, args.rule_key, args.field, kind)

    print(f"Wrote {rule_path}")
    for path in fixture_paths:
        print(f"Wrote {path}")
    print(
        "\nNext: review the generated `logic:` block (especially if this rule needs anything "
        "beyond a presence/non-blank check), then run the pack's own test suite - no Python "
        "changes needed for either step. See docs/rules-authoring.md."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
