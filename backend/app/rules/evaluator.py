"""The rule evaluator (P4-T3): pure `evaluate(facts, rules, as_of=...)`.

Three guarantees this module exists to provide, in order:

1. **Effective-date version selection.** `rules` may contain more than one
   version of the same `rule_key` (a regulation that changed over time); for
   each `rule_key`, `evaluate()` selects whichever version's
   `[effective_from, effective_to]` window contains `as_of` before doing
   anything else - "a 2024 label is judged by 2024 law" (IMPLEMENTATION.md
   section 9). A `rule_key` with no version effective at `as_of` is silently
   excluded, not marked with any finding: as a matter of law, that
   regulation did not exist yet (or no longer does), which is a different
   fact than "this rule doesn't apply to this label."
2. **Applicability gating**, evaluated *before* logic: a rule whose
   `jurisdiction`/`category` don't match, or whose `applicability.predicates`
   don't all hold, produces a `not_applicable` finding with a stated reason
   rather than being silently skipped - so a report can prove *why* a rule
   wasn't checked, not just that it wasn't.
3. **`insufficient_data` enforcement**, engine-level, not per rule: for an
   applicable rule, if any of `requires_fields` resolves to a missing value,
   the finding is `insufficient_data` and `logic` is never evaluated at all -
   there is no code path from "missing data" to "pass."

No I/O, no ambient clock: `as_of` is a required, caller-supplied argument -
this module never calls `date.today()`/`datetime.now()` itself, and imports
nothing that could reach the filesystem, network, or a database. Verified by
`tests/unit/test_rules_evaluator.py::test_evaluator_module_has_no_io_or_ambient_clock_access`,
a source-level import/call scan (this codebase has no `import-linter` package
configured, so "an import-lint test" is implemented directly as a plain,
dependency-free test rather than pulling in a new tool for one file).

Scope decision, documented rather than silently absent: IMPLEMENTATION.md
section 9's flagship example rule (`IN-FSSAI-FOOD-ALLERGEN-DECL`) uses a
`for_each ... where ... assert: {field_matches: {contains_ref: "$item.x"}}`
pattern that cross-references one `for_each` item's field into a sibling
predicate call outside that item's own scope. That specific cross-reference
mechanism is illustrative DSL sugar, not something P4-T3's own Tests/
Acceptance lines require, and building it well deserves its own pass once
real rule content (P4-T5) shows what's actually needed - so this evaluator's
`for_each` evaluates `where`/`assert` against each item's *own* scope only.
Predicates that need data external to the current scope (e.g.
`in_allergen_dictionary`'s allergen dictionary) instead take it via
`evaluate()`'s `predicate_kwargs`, merged into every call of that predicate
by name - the same "caller supplies external data explicitly" pattern
`app.rules.predicates` already uses.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from typing import Any

from app.rules.predicates import PredicateArgumentError, get_predicate, resolve_path
from app.rules.schema import Rule, Severity


class FindingStatus(StrEnum):
    PASS = "pass"  # noqa: S105 - a finding status, not a credential
    FAIL = "fail"
    INSUFFICIENT_DATA = "insufficient_data"
    NOT_APPLICABLE = "not_applicable"


@dataclass(slots=True, frozen=True)
class Finding:
    rule_key: str
    rule_version: int
    severity: Severity
    status: FindingStatus
    message: str | None
    reason: str | None
    evidence_fields: tuple[str, ...]


def _select_effective_rules(rules: Iterable[Rule], as_of: date) -> list[Rule]:
    by_key: dict[str, Rule] = {}
    for rule in rules:
        if rule.effective_from > as_of:
            continue
        if rule.effective_to is not None and rule.effective_to < as_of:
            continue
        current = by_key.get(rule.rule_key)
        if current is None or rule.version > current.version:
            by_key[rule.rule_key] = rule
    return list(by_key.values())


def _eval_predicate_call(
    node: Mapping[str, Any], scope: Any, predicate_kwargs: Mapping[str, Mapping[str, Any]]
) -> bool:
    (name, argument), = node.items()
    predicate = get_predicate(name)
    extra = predicate_kwargs.get(name, {})
    if isinstance(argument, dict):
        return bool(predicate(scope, **{**argument, **extra}))
    if isinstance(argument, str):
        return bool(predicate(scope, path=argument, **extra))
    # A bare non-string scalar (e.g. `some_predicate: true`): the predicate
    # is expected to interpret the current scope itself (path="").
    return bool(predicate(scope, path="", **extra))


def evaluate_logic(
    node: Mapping[str, Any],
    scope: Any,
    *,
    predicate_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
) -> bool:
    """Evaluate an already-validated (`app.rules.schema.validate_logic_node`)
    logic-tree node against `scope`. Exposed directly (not just via
    `evaluate()`) so a single node can be tested in isolation."""
    predicate_kwargs = predicate_kwargs or {}
    (key, value), = node.items()

    if key == "all":
        return all(
            evaluate_logic(child, scope, predicate_kwargs=predicate_kwargs) for child in value
        )
    if key == "any":
        return any(
            evaluate_logic(child, scope, predicate_kwargs=predicate_kwargs) for child in value
        )
    if key == "not":
        return not evaluate_logic(value, scope, predicate_kwargs=predicate_kwargs)
    if key == "for_each":
        items = resolve_path(scope, value["source"])
        if not isinstance(items, list | tuple):
            return False
        where = value.get("where")
        matched = [
            item
            for item in items
            if where is None or evaluate_logic(where, item, predicate_kwargs=predicate_kwargs)
        ]
        # Vacuously true for an empty (or fully-filtered-out) collection -
        # "every matching item satisfies X" holds trivially when there are
        # no matching items.
        return all(
            evaluate_logic(value["assert"], item, predicate_kwargs=predicate_kwargs)
            for item in matched
        )
    return _eval_predicate_call(node, scope, predicate_kwargs)


def evaluate(
    facts: Mapping[str, Any],
    rules: Iterable[Rule],
    *,
    as_of: date,
    jurisdiction: str,
    category: str,
    predicate_kwargs: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Finding]:
    """Evaluate every rule effective at `as_of` against `facts`, for a
    label already classified as `jurisdiction`/`category` (see P3-T9).
    Pure: no I/O, no ambient clock - `as_of` is always the caller's."""
    predicate_kwargs = predicate_kwargs or {}
    findings: list[Finding] = []

    for rule in _select_effective_rules(rules, as_of):
        applicable, reason = _check_applicability(
            rule, facts, jurisdiction, category, predicate_kwargs
        )
        if not applicable:
            findings.append(
                Finding(
                    rule_key=rule.rule_key,
                    rule_version=rule.version,
                    severity=rule.severity,
                    status=FindingStatus.NOT_APPLICABLE,
                    message=None,
                    reason=reason,
                    evidence_fields=tuple(rule.evidence.fields),
                )
            )
            continue

        missing = [f for f in rule.requires_fields if resolve_path(facts, f) is None]
        if missing:
            findings.append(
                Finding(
                    rule_key=rule.rule_key,
                    rule_version=rule.version,
                    severity=rule.severity,
                    status=FindingStatus.INSUFFICIENT_DATA,
                    message=rule.message.insufficient_data,
                    reason=f"missing required field(s): {sorted(missing)}",
                    evidence_fields=tuple(rule.evidence.fields),
                )
            )
            continue

        try:
            passed = evaluate_logic(rule.logic, facts, predicate_kwargs=predicate_kwargs)
        except PredicateArgumentError as exc:
            raise PredicateArgumentError(f"{rule.rule_key}: {exc}") from exc

        findings.append(
            Finding(
                rule_key=rule.rule_key,
                rule_version=rule.version,
                severity=rule.severity,
                status=FindingStatus.PASS if passed else FindingStatus.FAIL,
                message=(rule.message.pass_ if passed else rule.message.fail),
                reason=None,
                evidence_fields=tuple(rule.evidence.fields),
            )
        )

    return findings


def _check_applicability(
    rule: Rule,
    facts: Mapping[str, Any],
    jurisdiction: str,
    category: str,
    predicate_kwargs: Mapping[str, Mapping[str, Any]],
) -> tuple[bool, str | None]:
    if jurisdiction not in rule.applicability.jurisdiction:
        return False, f"jurisdiction {jurisdiction!r} is not in {rule.applicability.jurisdiction}."
    if category not in rule.applicability.category:
        return False, f"category {category!r} is not in {rule.applicability.category}."
    for predicate_node in rule.applicability.predicates:
        if not evaluate_logic(predicate_node, facts, predicate_kwargs=predicate_kwargs):
            (name, _), = predicate_node.items()
            return False, f"applicability predicate {name!r} did not match."
    return True, None
