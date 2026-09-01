"""Unit tests for the rule evaluator (P4-T3).

No live services needed - `evaluate()` is a pure function; `as_of` is always
supplied by the test, never read from the system clock.
"""

from __future__ import annotations

import ast
import inspect
from datetime import date

import pytest

from app.rules import evaluator as evaluator_module
from app.rules.evaluator import Finding, FindingStatus, evaluate, evaluate_logic
from app.rules.predicates import PredicateArgumentError
from app.rules.schema import Rule

pytestmark = pytest.mark.unit

TODAY = date(2024, 1, 1)


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "rule_key": "IN-TEST-RULE",
        "version": 1,
        "title": "Test rule",
        "citation": "c",
        "severity": "major",
        "effective_from": "2020-01-01",
        "effective_to": None,
        "applicability": {
            "jurisdiction": ["IN"],
            "category": ["packaged_food"],
            "predicates": [],
        },
        "logic": {"field_present": "quantity.net_quantity"},
        "requires_fields": ["quantity.net_quantity"],
        "on_missing_fields": "insufficient_data",
        "message": {
            "fail": "Net quantity is not declared.",
            "insufficient_data": "Could not read.",
        },
        "evidence": {"fields": ["quantity.net_quantity"]},
    }
    base.update(overrides)
    return Rule.model_validate(base)


def _fact(value: object) -> dict[str, object]:
    return {"value": value, "not_found_reason": None if value is not None else "missing"}


def _ev(
    facts: object,
    rules: list[Rule],
    *,
    as_of: date = TODAY,
    jurisdiction: str = "IN",
    category: str = "packaged_food",
    predicate_kwargs: dict[str, dict[str, object]] | None = None,
) -> list[Finding]:
    return evaluate(
        facts,  # type: ignore[arg-type]
        rules,
        as_of=as_of,
        jurisdiction=jurisdiction,
        category=category,
        predicate_kwargs=predicate_kwargs,
    )


FACTS_PRESENT = {"quantity": {"net_quantity": _fact("250 g")}}
FACTS_MISSING = {"quantity": {"net_quantity": _fact(None)}}


class TestInsufficientDataEnforcement:
    """The engine-level guarantee: missing required data never reaches
    'pass', regardless of what the logic tree would otherwise say."""

    def test_a_missing_required_field_yields_insufficient_data_not_pass(self) -> None:
        rule = _rule(logic={"field_present": "quantity.net_quantity"})
        findings = _ev(FACTS_MISSING, [rule])
        assert len(findings) == 1
        assert findings[0].status is FindingStatus.INSUFFICIENT_DATA
        assert findings[0].status is not FindingStatus.PASS
        assert findings[0].message == "Could not read."

    def test_logic_that_would_evaluate_true_on_missing_data_is_never_reached(self) -> None:
        # `not field_present` would be TRUE on missing data if logic ran -
        # insufficient_data must still win, proving requires_fields is
        # checked before logic, not as a fallback after it.
        rule = _rule(
            logic={"not": {"field_present": "quantity.net_quantity"}},
            requires_fields=["quantity.net_quantity"],
        )
        findings = _ev(FACTS_MISSING, [rule])
        assert findings[0].status is FindingStatus.INSUFFICIENT_DATA

    def test_multiple_missing_fields_are_all_named_in_the_reason(self) -> None:
        rule = _rule(
            requires_fields=["quantity.net_quantity", "dates.expiry_or_best_before"],
            logic={"field_present": "quantity.net_quantity"},
        )
        facts = {**FACTS_MISSING, "dates": {"expiry_or_best_before": _fact(None)}}
        findings = _ev(facts, [rule])
        assert "quantity.net_quantity" in findings[0].reason
        assert "dates.expiry_or_best_before" in findings[0].reason

    def test_no_required_fields_means_logic_always_runs(self) -> None:
        rule = _rule(requires_fields=[], logic={"field_present": "quantity.net_quantity"})
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].status is FindingStatus.PASS


class TestPassFail:
    def test_true_logic_over_present_data_is_pass(self) -> None:
        rule = _rule(logic={"field_present": "quantity.net_quantity"})
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].status is FindingStatus.PASS
        assert findings[0].reason is None

    def test_false_logic_over_present_data_is_fail(self) -> None:
        rule = _rule(logic={"not": {"field_present": "quantity.net_quantity"}})
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].status is FindingStatus.FAIL
        assert findings[0].message == "Net quantity is not declared."


class TestApplicabilityGating:
    def test_jurisdiction_mismatch_is_not_applicable_with_a_reason(self) -> None:
        rule = _rule()
        findings = _ev(FACTS_PRESENT, [rule], jurisdiction="EU")
        assert findings[0].status is FindingStatus.NOT_APPLICABLE
        assert "jurisdiction" in findings[0].reason
        assert findings[0].message is None

    def test_category_mismatch_is_not_applicable_with_a_reason(self) -> None:
        rule = _rule()
        findings = _ev(FACTS_PRESENT, [rule], category="cosmetics")
        assert findings[0].status is FindingStatus.NOT_APPLICABLE
        assert "category" in findings[0].reason

    def test_an_unmet_applicability_predicate_is_not_applicable(self) -> None:
        rule = _rule(
            applicability={
                "jurisdiction": ["IN"],
                "category": ["packaged_food"],
                "predicates": [{"field_present": "dates.manufacture_date"}],
            }
        )
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].status is FindingStatus.NOT_APPLICABLE
        assert "field_present" in findings[0].reason

    def test_applicability_is_checked_before_insufficient_data(self) -> None:
        # A rule that is both not-applicable AND missing its required field
        # must report not_applicable, since applicability gating runs first.
        rule = _rule()
        findings = _ev(FACTS_MISSING, [rule], jurisdiction="EU")
        assert findings[0].status is FindingStatus.NOT_APPLICABLE

    def test_a_met_applicability_predicate_lets_evaluation_proceed(self) -> None:
        rule = _rule(
            applicability={
                "jurisdiction": ["IN"],
                "category": ["packaged_food"],
                "predicates": [{"field_present": "quantity.net_quantity"}],
            }
        )
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].status is FindingStatus.PASS


class TestEffectiveDateVersionSelection:
    def _versioned_rule(
        self, version: int, effective_from: str, effective_to: str | None = None
    ) -> Rule:
        return _rule(
            rule_key="IN-TEST-VERSIONED",
            version=version,
            effective_from=effective_from,
            effective_to=effective_to,
            message={"fail": f"fail-v{version}", "insufficient_data": "x"},
        )

    def test_as_of_selects_the_historically_correct_version(self) -> None:
        v1 = self._versioned_rule(1, "2020-01-01", "2022-12-31")
        v2 = self._versioned_rule(2, "2023-01-01")

        in_2021 = _ev(FACTS_MISSING, [v1, v2], as_of=date(2021, 6, 1))
        in_2024 = _ev(FACTS_MISSING, [v1, v2], as_of=date(2024, 6, 1))
        assert [f.rule_version for f in in_2021] == [1]
        assert [f.rule_version for f in in_2024] == [2]

    def test_a_date_before_any_version_existed_yields_no_finding_for_that_rule_key(self) -> None:
        v1 = self._versioned_rule(1, "2020-01-01")
        findings = _ev(FACTS_MISSING, [v1], as_of=date(2019, 1, 1))
        assert findings == []

    def test_a_date_after_an_expired_version_with_no_successor_yields_no_finding(self) -> None:
        v1 = self._versioned_rule(1, "2020-01-01", "2022-12-31")
        findings = _ev(FACTS_MISSING, [v1], as_of=date(2023, 6, 1))
        assert findings == []

    def test_the_boundary_dates_are_inclusive(self) -> None:
        v1 = self._versioned_rule(1, "2020-01-01", "2022-12-31")
        on_start = _ev(FACTS_MISSING, [v1], as_of=date(2020, 1, 1))
        on_end = _ev(FACTS_MISSING, [v1], as_of=date(2022, 12, 31))
        assert len(on_start) == 1
        assert len(on_end) == 1

    def test_unrelated_rule_keys_are_unaffected_by_each_others_versioning(self) -> None:
        v1 = self._versioned_rule(1, "2020-01-01", "2022-12-31")
        other = _rule(rule_key="IN-TEST-OTHER", version=1, effective_from="2020-01-01")
        findings = _ev(FACTS_PRESENT, [v1, other])
        assert {f.rule_key for f in findings} == {"IN-TEST-OTHER"}


class TestForEachLogic:
    def test_for_each_is_vacuously_true_over_an_empty_source(self) -> None:
        scope = {"items": []}
        node = {"for_each": {"source": "items", "assert": {"field_present": "name"}}}
        assert evaluate_logic(node, scope) is True

    def test_for_each_assert_runs_against_each_items_own_scope(self) -> None:
        scope = {"items": [{"name": "Milk"}, {"name": "Soy"}]}
        node = {"for_each": {"source": "items", "assert": {"field_present": "name"}}}
        assert evaluate_logic(node, scope) is True

    def test_for_each_fails_if_any_item_fails_the_assertion(self) -> None:
        scope = {"items": [{"name": "Milk"}, {"other": "no name field"}]}
        node = {"for_each": {"source": "items", "assert": {"field_present": "name"}}}
        assert evaluate_logic(node, scope) is False

    def test_for_each_where_filters_which_items_are_asserted_on(self) -> None:
        scope = {"items": [{"name": "Milk", "flag": True}, {"name": "Water", "flag": False}]}
        node = {
            "for_each": {
                "source": "items",
                "where": {"field_matches": {"path": "flag", "equals": True}},
                "assert": {"field_matches": {"path": "name", "equals": "Milk"}},
            }
        }
        assert evaluate_logic(node, scope) is True

    def test_for_each_over_a_non_list_source_is_false(self) -> None:
        node = {"for_each": {"source": "not_a_list", "assert": {"field_present": "x"}}}
        assert evaluate_logic(node, {"not_a_list": "a string"}) is False


class TestCombinators:
    def test_all_requires_every_child_true(self) -> None:
        scope = {"a": 1, "b": None}
        assert evaluate_logic({"all": [{"field_present": "a"}]}, scope) is True
        both = {"all": [{"field_present": "a"}, {"field_present": "b"}]}
        assert evaluate_logic(both, scope) is False

    def test_any_requires_at_least_one_child_true(self) -> None:
        scope = {"a": 1, "b": None}
        either = {"any": [{"field_present": "a"}, {"field_present": "b"}]}
        assert evaluate_logic(either, scope) is True
        assert evaluate_logic({"any": [{"field_present": "b"}]}, scope) is False

    def test_not_inverts_its_child(self) -> None:
        scope = {"a": 1}
        assert evaluate_logic({"not": {"field_present": "a"}}, scope) is False
        assert evaluate_logic({"not": {"field_present": "missing"}}, scope) is True


class TestBareScalarPredicateCall:
    def test_a_bare_non_string_argument_checks_the_scope_itself(self) -> None:
        # `{some_predicate: true}` - the value `true` is just YAML-truthy
        # sugar; the predicate interprets the current scope directly (path="").
        node = {"in_allergen_dictionary": True}
        kwargs = {"in_allergen_dictionary": {"dictionary": {"milk": "milk"}}}
        assert evaluate_logic(node, "Milk", predicate_kwargs=kwargs) is True


class TestPredicateKwargsInjection:
    def test_extra_kwargs_are_merged_into_matching_predicate_calls(self) -> None:
        scope = {"item": "Milk"}
        node = {"in_allergen_dictionary": {"path": "item"}}
        dictionary = {"milk": "milk"}
        kwargs = {"in_allergen_dictionary": {"dictionary": dictionary}}
        assert evaluate_logic(node, scope, predicate_kwargs=kwargs) is True

    def test_a_realistic_allergen_style_rule_end_to_end(self) -> None:
        rule = _rule(
            rule_key="IN-FSSAI-FOOD-ALLERGEN-DECL",
            applicability={
                "jurisdiction": ["IN"],
                "category": ["packaged_food"],
                "predicates": [{"field_present": "ingredients.items"}],
            },
            logic={
                "for_each": {
                    "source": "ingredients.items",
                    "where": {"in_allergen_dictionary": {"path": "name"}},
                    "assert": {"field_matches": {"path": "name", "equals": "Milk"}},
                }
            },
            requires_fields=["ingredients.items"],
            message={"fail": "undeclared allergen", "insufficient_data": "x"},
            evidence={"fields": ["ingredients.items"]},
        )
        facts = {
            "ingredients": {
                "items": _fact(
                    [
                        {"name": "Wheat Flour", "position": 0, "percentage": None},
                        {"name": "Milk", "position": 1, "percentage": None},
                    ]
                )
            }
        }
        dictionary = {"wheat flour": "wheat", "milk": "milk"}
        findings = _ev(
            facts, [rule], predicate_kwargs={"in_allergen_dictionary": {"dictionary": dictionary}}
        )
        # "Wheat Flour" is in the allergen dictionary (where holds) but its
        # own name isn't "Milk" (assert fails for that item) -> overall fail,
        # exactly matching this evaluator's documented item-scoped for_each
        # semantics (see the module docstring's scope decision).
        assert findings[0].status is FindingStatus.FAIL


class TestPredicateArgumentErrorsPropagateWithContext:
    def test_a_malformed_predicate_call_raises_with_the_rule_key_attached(self) -> None:
        rule = _rule(logic={"numeric_within": {"path": "quantity.net_quantity"}})  # no min/max
        with pytest.raises(PredicateArgumentError, match="IN-TEST-RULE"):
            _ev(FACTS_PRESENT, [rule])


class TestFindingShape:
    def test_findings_are_frozen(self) -> None:
        finding = Finding(
            rule_key="X",
            rule_version=1,
            severity=_rule().severity,
            status=FindingStatus.PASS,
            message=None,
            reason=None,
            evidence_fields=(),
        )
        with pytest.raises(Exception):  # noqa: B017 - dataclasses.FrozenInstanceError
            finding.status = FindingStatus.FAIL  # type: ignore[misc]

    def test_evidence_fields_come_from_the_rule(self) -> None:
        fields = ["quantity.net_quantity", "dates.expiry_or_best_before"]
        rule = _rule(evidence={"fields": fields})
        findings = _ev(FACTS_PRESENT, [rule])
        assert findings[0].evidence_fields == tuple(fields)


class TestNoIoOrAmbientClockAccess:
    """P4-T3's acceptance criterion, literally: "the evaluator has no I/O and
    no ambient clock access (enforced by an import-lint test)." This project
    has no `import-linter` package configured, so the check is implemented
    directly as a static source scan rather than pulling in a new tool for
    one file."""

    _FORBIDDEN_IMPORT_ROOTS = frozenset(
        {
            "os",
            "sys",
            "pathlib",
            "io",
            "socket",
            "requests",
            "httpx",
            "boto3",
            "botocore",
            "sqlalchemy",
            "psycopg",
            "redis",
            "time",
        }
    )
    _FORBIDDEN_CALL_NAMES = frozenset({"now", "today", "utcnow", "time", "perf_counter", "open"})

    def test_evaluator_module_has_no_io_or_ambient_clock_access(self) -> None:
        source = inspect.getsource(evaluator_module)
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    msg = f"forbidden import: {alias.name}"
                    assert root not in self._FORBIDDEN_IMPORT_ROOTS, msg
            elif isinstance(node, ast.ImportFrom) and node.module:
                root = node.module.split(".")[0]
                assert root not in self._FORBIDDEN_IMPORT_ROOTS, f"forbidden import: {node.module}"
            elif isinstance(node, ast.Call):
                target = node.func
                is_attr = isinstance(target, ast.Attribute)
                name = target.attr if is_attr else getattr(target, "id", None)
                assert name not in self._FORBIDDEN_CALL_NAMES, f"forbidden call: {name}"

    def test_evaluate_is_deterministic_given_the_same_as_of(self) -> None:
        rule = _rule()
        assert _ev(FACTS_PRESENT, [rule]) == _ev(FACTS_PRESENT, [rule])
