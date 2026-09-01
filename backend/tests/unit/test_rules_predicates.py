"""Unit tests for the closed predicate library (P4-T2).

No live services needed - every predicate is pure. Property-style
(boundary/table-driven) tests cover the numeric and unit predicates per
this task's own Tests line.
"""

from __future__ import annotations

import pytest

from app.rules.loader import PackValidationError, load_pack
from app.rules.predicates import (
    PREDICATES,
    PredicateArgumentError,
    UnregisteredPredicateError,
    collect_predicate_names,
    date_valid,
    field_matches,
    field_present,
    get_predicate,
    in_allergen_dictionary,
    language_present,
    min_font_size_mm,
    numeric_within,
    regex_matches,
    resolve_path,
    set_contains,
    unit_convertible_to,
    validate_predicates_registered,
)
from app.rules.schema import Rule

pytestmark = pytest.mark.unit


class TestResolvePath:
    def test_resolves_a_nested_plain_value(self) -> None:
        scope = {"a": {"b": {"c": 42}}}
        assert resolve_path(scope, "a.b.c") == 42

    def test_unwraps_the_fact_wrapper_convention(self) -> None:
        scope = {"quantity": {"net_quantity": {"value": "250 g", "not_found_reason": None}}}
        assert resolve_path(scope, "quantity.net_quantity") == "250 g"

    def test_a_missing_path_returns_none(self) -> None:
        assert resolve_path({"a": {"b": 1}}, "a.c") is None
        assert resolve_path({"a": 1}, "a.b") is None

    def test_a_fact_wrapper_with_no_value_resolves_to_none(self) -> None:
        scope = {"x": {"value": None, "not_found_reason": "not on label"}}
        assert resolve_path(scope, "x") is None

    def test_an_empty_path_returns_the_scope_itself(self) -> None:
        # Used by the evaluator's for_each: a scalar loop item (not a
        # mapping) becomes its own scope, addressed with path="".
        assert resolve_path("Milk", "") == "Milk"
        assert resolve_path({"a": 1}, "") == {"a": 1}


class TestFieldPresent:
    def test_true_when_value_is_present(self) -> None:
        assert field_present({"x": {"value": "y", "not_found_reason": None}}, path="x") is True

    def test_false_when_missing_via_fact_wrapper(self) -> None:
        assert field_present({"x": {"value": None, "not_found_reason": "why"}}, path="x") is False

    def test_false_when_path_does_not_exist(self) -> None:
        assert field_present({}, path="a.b") is False


class TestFieldMatches:
    def test_equals(self) -> None:
        assert field_matches({"x": "milk"}, path="x", equals="milk") is True
        assert field_matches({"x": "milk"}, path="x", equals="soy") is False

    def test_contains_backs_the_dsl_contains_ref_usage(self) -> None:
        scope = {"allergens": {"declared": ["Milk", "Soy"]}}
        assert field_matches(scope, path="allergens.declared", contains="Milk") is True
        assert field_matches(scope, path="allergens.declared", contains="Wheat") is False

    def test_pattern(self) -> None:
        assert field_matches({"x": "ABC-123"}, path="x", pattern=r"^ABC-\d+$") is True
        assert field_matches({"x": "nope"}, path="x", pattern=r"^ABC-\d+$") is False

    def test_missing_value_is_false_regardless_of_mode(self) -> None:
        assert field_matches({}, path="x", equals="anything") is False

    def test_requires_exactly_one_of_equals_contains_pattern(self) -> None:
        with pytest.raises(PredicateArgumentError):
            field_matches({"x": "y"}, path="x")
        with pytest.raises(PredicateArgumentError):
            field_matches({"x": "y"}, path="x", equals="y", pattern="y")

    def test_contains_mode_with_a_non_container_value_is_false(self) -> None:
        assert field_matches({"x": "not a list"}, path="x", contains="y") is False

    def test_pattern_mode_with_a_non_string_value_is_false(self) -> None:
        assert field_matches({"x": 42}, path="x", pattern=r"\d+") is False


class TestRegexMatches:
    @pytest.mark.parametrize(
        ("value", "pattern", "expected"),
        [
            ("2027-12-25", r"^\d{4}-\d{2}-\d{2}$", True),
            ("not a date", r"^\d{4}-\d{2}-\d{2}$", False),
            ("ABC123", r"[A-Z]+\d+", True),
        ],
    )
    def test_matches(self, value: str, pattern: str, expected: bool) -> None:
        assert regex_matches({"x": value}, path="x", pattern=pattern) is expected

    def test_non_string_value_is_false(self) -> None:
        assert regex_matches({"x": 42}, path="x", pattern=r"\d+") is False


class TestNumericWithin:
    @pytest.mark.parametrize(
        ("value", "minimum", "maximum", "expected"),
        [
            (50, 0, 100, True),
            (0, 0, 100, True),  # inclusive lower boundary
            (100, 0, 100, True),  # inclusive upper boundary
            (-1, 0, 100, False),
            (101, 0, 100, False),
            (5, 10, None, False),
            (10, 10, None, True),  # inclusive with only a minimum
            (5, None, 10, True),
            (11, None, 10, False),
            (2.5, 0, 5, True),
        ],
    )
    def test_boundaries(
        self, value: float, minimum: float | None, maximum: float | None, expected: bool
    ) -> None:
        assert (
            numeric_within({"x": value}, path="x", minimum=minimum, maximum=maximum) is expected
        )

    def test_non_numeric_value_is_false(self) -> None:
        assert numeric_within({"x": "50"}, path="x", minimum=0, maximum=100) is False

    def test_bool_is_not_treated_as_numeric(self) -> None:
        # bool is a subclass of int in Python - explicitly excluded.
        assert numeric_within({"x": True}, path="x", minimum=0, maximum=1) is False

    def test_requires_at_least_one_bound(self) -> None:
        with pytest.raises(PredicateArgumentError):
            numeric_within({"x": 5}, path="x")


class TestMinFontSizeMm:
    @pytest.mark.parametrize(
        ("value", "minimum", "expected"),
        [(1.2, 1.2, True), (1.19, 1.2, False), (5.0, 1.2, True), (0.0, 1.2, False)],
    )
    def test_boundaries(self, value: float, minimum: float, expected: bool) -> None:
        assert min_font_size_mm({"x": value}, path="x", minimum=minimum) is expected

    def test_missing_value_is_false(self) -> None:
        assert min_font_size_mm({}, path="x", minimum=1.2) is False


class TestUnitConvertibleTo:
    @pytest.mark.parametrize(
        "unit", ["g", "mg", "kg", "mcg", "µg", "GRAM", " g ", "Kilogram"]
    )
    def test_recognized_mass_units(self, unit: str) -> None:
        assert unit_convertible_to({"x": unit}, path="x", dimension="mass") is True
        assert unit_convertible_to({"x": unit}, path="x", dimension="volume") is False

    @pytest.mark.parametrize("unit", ["ml", "l", "cl", "Litre", " ML "])
    def test_recognized_volume_units(self, unit: str) -> None:
        assert unit_convertible_to({"x": unit}, path="x", dimension="volume") is True
        assert unit_convertible_to({"x": unit}, path="x", dimension="mass") is False

    def test_unrecognized_unit_is_false(self) -> None:
        assert unit_convertible_to({"x": "furlongs"}, path="x", dimension="mass") is False

    def test_non_string_value_is_false(self) -> None:
        assert unit_convertible_to({"x": 5}, path="x", dimension="mass") is False

    def test_unknown_dimension_raises(self) -> None:
        with pytest.raises(PredicateArgumentError):
            unit_convertible_to({"x": "g"}, path="x", dimension="length")


class TestSetContains:
    def test_membership(self) -> None:
        scope = {"allergens": {"declared": ["Milk", "Soy"]}}
        assert set_contains(scope, path="allergens.declared", value="Milk") is True
        assert set_contains(scope, path="allergens.declared", value="Wheat") is False

    def test_non_container_value_is_false(self) -> None:
        assert set_contains({"x": "Milk"}, path="x", value="Milk") is False


class TestLanguagePresent:
    def test_membership(self) -> None:
        scope = {"languages": {"detected": ["en", "hi"]}}
        assert language_present(scope, path="languages.detected", language="en") is True
        assert language_present(scope, path="languages.detected", language="fr") is False

    def test_non_list_value_is_false(self) -> None:
        assert language_present({"x": "en"}, path="x", language="en") is False


class TestDateValid:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("2027-12-25", True),
            ("2027-12", True),  # month/year-only, per P3-T7's date convention
            ("2027-13", False),  # invalid month
            ("2027-02-30", False),  # invalid day
            ("not a date", False),
            ("25/12/2027", False),  # not ISO - this predicate checks ISO shape only
        ],
    )
    def test_validity(self, value: str, expected: bool) -> None:
        assert date_valid({"x": value}, path="x") is expected

    def test_non_string_value_is_false(self) -> None:
        assert date_valid({"x": 20271225}, path="x") is False


class TestInAllergenDictionary:
    DICTIONARY = {"milk": "milk", "soya": "soybeans", "wheat flour": "wheat"}

    def test_a_known_synonym_is_recognized(self) -> None:
        assert (
            in_allergen_dictionary({"x": "Milk"}, path="x", dictionary=self.DICTIONARY) is True
        )
        assert (
            in_allergen_dictionary({"x": "wheat flour"}, path="x", dictionary=self.DICTIONARY)
            is True
        )

    def test_an_unknown_word_is_not_recognized(self) -> None:
        assert in_allergen_dictionary({"x": "water"}, path="x", dictionary=self.DICTIONARY) is False

    def test_non_string_value_is_false(self) -> None:
        assert in_allergen_dictionary({"x": 5}, path="x", dictionary=self.DICTIONARY) is False


class TestRegistry:
    def test_every_named_predicate_from_implementation_md_is_registered(self) -> None:
        for name in (
            "field_present",
            "regex_matches",
            "min_font_size_mm",
            "numeric_within",
            "unit_convertible_to",
            "set_contains",
            "language_present",
            "date_valid",
        ):
            assert name in PREDICATES

    def test_the_demo_rules_predicates_are_registered_too(self) -> None:
        assert "field_matches" in PREDICATES
        assert "in_allergen_dictionary" in PREDICATES

    def test_get_predicate_returns_the_callable(self) -> None:
        assert get_predicate("field_present") is field_present

    def test_get_predicate_raises_for_an_unregistered_name(self) -> None:
        with pytest.raises(UnregisteredPredicateError, match="not_a_real_predicate"):
            get_predicate("not_a_real_predicate")


class TestCollectPredicateNames:
    def test_flat_predicate(self) -> None:
        assert collect_predicate_names({"field_present": "x"}) == {"field_present"}

    @pytest.mark.parametrize("node", [{}, {"a": 1, "b": 2}, "not a mapping", 5])
    def test_a_malformed_node_yields_no_names_rather_than_crashing(self, node: object) -> None:
        assert collect_predicate_names(node) == set()

    def test_nested_combinators(self) -> None:
        node = {
            "all": [
                {"any": [{"field_present": "a"}, {"not": {"regex_matches": {"path": "b"}}}]},
                {
                    "for_each": {
                        "source": "items",
                        "where": {"in_allergen_dictionary": {"path": "x"}},
                        "assert": {"set_contains": {"path": "y"}},
                    }
                },
            ]
        }
        assert collect_predicate_names(node) == {
            "field_present",
            "regex_matches",
            "in_allergen_dictionary",
            "set_contains",
        }


class TestValidatePredicatesRegistered:
    BASE_RULE: dict[str, object] = {
        "rule_key": "IN-TEST-RULE",
        "version": 1,
        "title": "t",
        "citation": "c",
        "severity": "minor",
        "effective_from": "2024-01-01",
        "applicability": {"jurisdiction": ["IN"], "category": ["packaged_food"], "predicates": []},
        "requires_fields": [],
        "on_missing_fields": "insufficient_data",
        "message": {"insufficient_data": "x"},
        "evidence": {"fields": ["x"]},
    }

    def test_a_rule_using_only_registered_predicates_passes(self) -> None:
        rule = Rule.model_validate({**self.BASE_RULE, "logic": {"field_present": "x"}})
        validate_predicates_registered(rule)  # does not raise

    def test_a_rule_using_an_unregistered_predicate_is_rejected(self) -> None:
        rule = Rule.model_validate({**self.BASE_RULE, "logic": {"made_up_predicate": "x"}})
        with pytest.raises(UnregisteredPredicateError, match="made_up_predicate"):
            validate_predicates_registered(rule)

    def test_an_unregistered_predicate_in_applicability_is_also_rejected(self) -> None:
        rule = Rule.model_validate(
            {
                **self.BASE_RULE,
                "applicability": {
                    "jurisdiction": ["IN"],
                    "category": ["packaged_food"],
                    "predicates": [{"not_a_real_predicate": "x"}],
                },
                "logic": {"field_present": "x"},
            }
        )
        with pytest.raises(UnregisteredPredicateError, match="not_a_real_predicate"):
            validate_predicates_registered(rule)

    def test_an_unregistered_predicate_makes_the_pack_fail_to_load(self) -> None:
        """The literal acceptance criterion, exercised through the actual
        pack loader a real publish path would use."""
        manifest = {
            "pack_id": "in-fssai-food",
            "jurisdiction": "IN",
            "category": "packaged_food",
            "version": "1.0.0",
            "effective_from": "2024-01-01",
            "source_citations": ["x"],
            "author": "t",
            "review_date": "2024-01-01",
        }
        bad_rule = {**self.BASE_RULE, "logic": {"totally_made_up": "x"}}
        with pytest.raises(PackValidationError) as exc_info:
            load_pack(manifest, [bad_rule])
        assert any("totally_made_up" in issue for issue in exc_info.value.issues)
