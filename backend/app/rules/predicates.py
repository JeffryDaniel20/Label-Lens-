"""The closed predicate library (P4-T2).

Every predicate is a pure function `(scope, **kwargs) -> bool` - no I/O, no
ambient clock, no randomness - registered in `PREDICATES`, a closed mapping
a rule's `logic` tree can reference by name. "Closed" is load-bearing: after
a `Rule` parses structurally (P4-T1), `validate_predicates_registered()`
walks its logic tree and rejects it if it names anything not in this
registry, which is this task's literal acceptance criterion - no rule can
invoke an unregistered predicate.

`scope` is a plain `Mapping[str, Any]`, not `app.extraction.facts.LabelFacts`
- this module takes no dependency on `extraction` (or any other LabelLens
module), matching `app.rules.schema`'s stance. The one case that looks like
an exception, `in_allergen_dictionary`, actually reinforces it: an allergen
list is regulation-defined content, and a *second*, separately-maintained
copy of it living inside the rule engine would be a real correctness hazard
in a compliance product (two lists that can silently drift apart), so rather
than import `app.extraction.normalize.allergens.ALLERGEN_SYNONYMS` here,
`in_allergen_dictionary` takes the dictionary as an explicit argument -
whatever calls it (the evaluator, or ultimately the `analysis` module that
the module table says is allowed to depend on "all pipeline modules") is
responsible for passing the one real dictionary in. Contrast
`unit_convertible_to`'s small mass/volume unit table below, which *is*
self-contained: unlike an allergen list, which regulation of the day, a unit
name's physical dimension is stable, non-regulatory knowledge, so
duplicating that much is a reasonable, deliberate simplification rather than
a drift risk.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import date
from types import MappingProxyType
from typing import Any

from app.rules.schema import Rule

_SENTINEL = object()

# Predicates have heterogeneous keyword-only signatures (e.g. `numeric_within`
# takes `minimum`/`maximum`, `field_matches` takes `equals`/`contains`/
# `pattern`), so a structural Protocol with `**kwargs: Any` can't express
# "any of these" - mypy rejects a specific-keyword function as incompatible
# with a `**kwargs` Protocol even though every call site here is valid.
PredicateFn = Callable[..., bool]


class PredicateArgumentError(ValueError):
    """A predicate was called with arguments it cannot make sense of."""


class UnregisteredPredicateError(ValueError):
    """A rule's logic tree references a predicate name not in `PREDICATES`."""


def resolve_path(scope: Mapping[str, Any], path: str) -> Any:
    """Navigate a dotted path through a nested mapping.

    If the node the path lands on looks like the fact-wrapper convention
    (`{"value": ..., "not_found_reason": ...}` - what `Fact.model_dump()`
    produces), returns just its `"value"`, so predicates never have to know
    about that wrapper themselves.
    """
    if path == "":
        return scope
    node: Any = scope
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    if (
        isinstance(node, Mapping)
        and "value" in node
        and "not_found_reason" in node
        and len(node) == 2
    ):
        return node["value"]
    return node


def _is_number(value: Any) -> bool:
    return isinstance(value, int | float) and not isinstance(value, bool)


def field_present(scope: Mapping[str, Any], *, path: str) -> bool:
    return resolve_path(scope, path) is not None


def field_matches(
    scope: Mapping[str, Any],
    *,
    path: str,
    equals: Any = _SENTINEL,
    contains: Any = _SENTINEL,
    pattern: str | None = None,
) -> bool:
    """Exactly one of `equals` / `contains` / `pattern` must be given.

    `contains` is what backs the DSL's `contains_ref` usage (see the
    `IN-FSSAI-FOOD-ALLERGEN-DECL` example in IMPLEMENTATION.md section 9):
    the evaluator resolves the `$item.allergen_key` reference to a concrete
    value first and passes it here as `contains` - this predicate itself
    never parses `$item...` references, keeping it a pure, context-free
    membership/equality/pattern check.
    """
    given = [
        name
        for name, value in (("equals", equals), ("contains", contains))
        if value is not _SENTINEL
    ]
    if pattern is not None:
        given.append("pattern")
    if len(given) != 1:
        raise PredicateArgumentError(
            "field_matches: exactly one of 'equals', 'contains', 'pattern' is required, "
            f"got {given or 'none'}."
        )

    value = resolve_path(scope, path)
    if value is None:
        return False

    if equals is not _SENTINEL:
        return bool(value == equals)
    if contains is not _SENTINEL:
        if not isinstance(value, list | tuple | set | frozenset):
            return False
        return bool(contains in value)
    if not isinstance(value, str):
        return False
    return re.search(pattern, value) is not None  # type: ignore[arg-type]


def regex_matches(scope: Mapping[str, Any], *, path: str, pattern: str) -> bool:
    value = resolve_path(scope, path)
    if not isinstance(value, str):
        return False
    return re.search(pattern, value) is not None


def numeric_within(
    scope: Mapping[str, Any],
    *,
    path: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> bool:
    if minimum is None and maximum is None:
        raise PredicateArgumentError("numeric_within: at least one of minimum/maximum is required.")
    value = resolve_path(scope, path)
    if not _is_number(value):
        return False
    if minimum is not None and value < minimum:
        return False
    if maximum is not None and value > maximum:
        return False
    return True


def min_font_size_mm(scope: Mapping[str, Any], *, path: str, minimum: float) -> bool:
    """No stage of this pipeline measures physical font size from a label
    image yet, so nothing currently populates a `font_size_mm`-shaped fact -
    this predicate is complete and tested against any numeric value at
    `path`, ready for whenever that measurement exists."""
    value = resolve_path(scope, path)
    if not _is_number(value):
        return False
    return bool(value >= minimum)


_MASS_UNITS = frozenset(
    {"mg", "g", "kg", "mcg", "µg", "ug", "milligram", "milligrams", "gram", "grams",
     "kilogram", "kilograms", "microgram", "micrograms"}
)
_VOLUME_UNITS = frozenset(
    {"ml", "l", "cl", "millilitre", "milliliter", "millilitres", "milliliters",
     "litre", "liter", "litres", "liters"}
)
_UNIT_DIMENSIONS: Mapping[str, frozenset[str]] = MappingProxyType(
    {"mass": _MASS_UNITS, "volume": _VOLUME_UNITS}
)


def unit_convertible_to(scope: Mapping[str, Any], *, path: str, dimension: str) -> bool:
    if dimension not in _UNIT_DIMENSIONS:
        raise PredicateArgumentError(
            f"unit_convertible_to: unknown dimension {dimension!r}, "
            f"expected one of {sorted(_UNIT_DIMENSIONS)}."
        )
    value = resolve_path(scope, path)
    if not isinstance(value, str):
        return False
    return value.strip().lower() in _UNIT_DIMENSIONS[dimension]


def set_contains(scope: Mapping[str, Any], *, path: str, value: Any) -> bool:
    container = resolve_path(scope, path)
    if not isinstance(container, list | tuple | set | frozenset):
        return False
    return value in container


def language_present(scope: Mapping[str, Any], *, path: str, language: str) -> bool:
    languages = resolve_path(scope, path)
    if not isinstance(languages, list | tuple | set | frozenset):
        return False
    return language in languages


def date_valid(scope: Mapping[str, Any], *, path: str) -> bool:
    value = resolve_path(scope, path)
    if not isinstance(value, str):
        return False
    if re.match(r"^\d{4}-\d{2}$", value):
        try:
            date(int(value[:4]), int(value[5:7]), 1)
        except ValueError:
            return False
        return True
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def in_allergen_dictionary(
    scope: Mapping[str, Any], *, path: str, dictionary: Mapping[str, str]
) -> bool:
    """`dictionary` is a synonym -> canonical-allergen mapping, e.g.
    `app.extraction.normalize.allergens.ALLERGEN_SYNONYMS` - the caller's
    responsibility to supply (see the module docstring)."""
    value = resolve_path(scope, path)
    if not isinstance(value, str):
        return False
    return dictionary.get(value.strip().lower()) is not None


PREDICATES: Mapping[str, PredicateFn] = MappingProxyType(
    {
        "field_present": field_present,
        "field_matches": field_matches,
        "regex_matches": regex_matches,
        "numeric_within": numeric_within,
        "min_font_size_mm": min_font_size_mm,
        "unit_convertible_to": unit_convertible_to,
        "set_contains": set_contains,
        "language_present": language_present,
        "date_valid": date_valid,
        "in_allergen_dictionary": in_allergen_dictionary,
    }
)


def get_predicate(name: str) -> PredicateFn:
    try:
        return PREDICATES[name]
    except KeyError:
        raise UnregisteredPredicateError(f"{name!r} is not a registered predicate.") from None


def collect_predicate_names(node: Any) -> set[str]:
    """Walk a logic-tree node (or an applicability predicate entry) and
    collect every predicate name it references, recursing through the
    `all`/`any`/`not`/`for_each` combinators."""
    if not isinstance(node, dict) or len(node) != 1:
        return set()
    (key, value), = node.items()
    if key in ("all", "any"):
        names: set[str] = set()
        for child in value:
            names |= collect_predicate_names(child)
        return names
    if key == "not":
        return collect_predicate_names(value)
    if key == "for_each":
        names = set()
        if "where" in value:
            names |= collect_predicate_names(value["where"])
        names |= collect_predicate_names(value["assert"])
        return names
    return {key}


def validate_predicates_registered(rule: Rule) -> None:
    """Raise `UnregisteredPredicateError` if `rule` references any predicate
    name not in `PREDICATES`, anywhere in its applicability or logic."""
    unregistered: set[str] = set()
    for predicate in rule.applicability.predicates:
        unregistered |= collect_predicate_names(predicate) - PREDICATES.keys()
    unregistered |= collect_predicate_names(rule.logic) - PREDICATES.keys()
    if unregistered:
        raise UnregisteredPredicateError(
            f"{rule.rule_key}: references unregistered predicate(s): {sorted(unregistered)}."
        )
