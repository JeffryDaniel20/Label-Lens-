"""Unit tests for the rule DSL schema and structural validation (P4-T1).

No live services needed - pure Pydantic/dict validation.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.rules.schema import (
    PackManifest,
    Rule,
    RuleParseError,
    Severity,
    validate_logic_node,
)

pytestmark = pytest.mark.unit

VALID_RULE: dict[str, object] = {
    "rule_key": "IN-FSSAI-FOOD-ALLERGEN-DECL",
    "version": 3,
    "title": "Declaration of major allergens",
    "citation": "FSS (Labelling & Display) Regulations, 2020 — reg. 5(6)",
    "severity": "critical",
    "effective_from": "2020-07-01",
    "effective_to": None,
    "applicability": {
        "jurisdiction": ["IN"],
        "category": ["packaged_food"],
        "predicates": [{"field_present": "ingredients.items"}],
    },
    "logic": {
        "all": [
            {
                "for_each": {
                    "source": "ingredients.items",
                    "where": {"in_allergen_dictionary": True},
                    "assert": {
                        "field_matches": {
                            "path": "allergens.declared",
                            "contains_ref": "$item.allergen_key",
                        }
                    },
                }
            }
        ]
    },
    "requires_fields": ["ingredients.items", "allergens.declared"],
    "on_missing_fields": "insufficient_data",
    "message": {
        "fail": "Allergen '{allergen}' appears in ingredients but is not declared.",
        "insufficient_data": "Ingredient list could not be read with sufficient confidence.",
    },
    "evidence": {"fields": ["ingredients.items", "allergens.declared"]},
}

VALID_MANIFEST: dict[str, object] = {
    "pack_id": "in-fssai-food",
    "jurisdiction": "IN",
    "category": "packaged_food",
    "version": "1.0.0",
    "effective_from": "2024-01-01",
    "source_citations": ["FSS (Labelling & Display) Regulations, 2020"],
    "author": "test-author",
    "review_date": "2024-01-01",
}


def _rule(**overrides: object) -> dict[str, object]:
    data = dict(VALID_RULE)
    data.update(overrides)
    return data


def _manifest(**overrides: object) -> dict[str, object]:
    data = dict(VALID_MANIFEST)
    data.update(overrides)
    return data


class TestValidRuleParses:
    def test_the_example_rule_from_implementation_md_parses(self) -> None:
        rule = Rule.model_validate(VALID_RULE)
        assert rule.rule_key == "IN-FSSAI-FOOD-ALLERGEN-DECL"
        assert rule.severity is Severity.CRITICAL
        assert rule.on_missing_fields == "insufficient_data"

    def test_rules_are_frozen(self) -> None:
        rule = Rule.model_validate(VALID_RULE)
        with pytest.raises(ValidationError):
            rule.version = 4  # type: ignore[misc]


class TestMalformedRuleRejection:
    """Each case must be rejected at load with a precise, identifiable error."""

    def test_invalid_severity_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="severity"):
            Rule.model_validate(_rule(severity="catastrophic"))

    def test_on_missing_fields_other_than_insufficient_data_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="on_missing_fields"):
            Rule.model_validate(_rule(on_missing_fields="pass"))

    def test_malformed_rule_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="rule_key"):
            Rule.model_validate(_rule(rule_key="not a valid key"))

    def test_effective_to_before_effective_from_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="effective_to"):
            Rule.model_validate(
                _rule(effective_from="2024-06-01", effective_to="2024-01-01")
            )

    def test_missing_required_field_is_rejected(self) -> None:
        data = _rule()
        del data["citation"]
        with pytest.raises(ValidationError, match="citation"):
            Rule.model_validate(data)

    def test_zero_or_negative_version_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Rule.model_validate(_rule(version=0))

    def test_empty_applicability_jurisdiction_is_rejected(self) -> None:
        applicability = dict(VALID_RULE["applicability"])  # type: ignore[arg-type]
        applicability["jurisdiction"] = []
        with pytest.raises(ValidationError):
            Rule.model_validate(_rule(applicability=applicability))

    def test_empty_evidence_fields_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            Rule.model_validate(_rule(evidence={"fields": []}))

    def test_malformed_field_path_in_requires_fields_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="requires_fields"):
            Rule.model_validate(_rule(requires_fields=["Not A Valid Path!"]))


class TestLogicTreeStructure:
    def test_a_bare_predicate_call_is_valid(self) -> None:
        validate_logic_node({"field_present": "ingredients.items"})

    def test_nested_all_any_not_for_each_is_valid(self) -> None:
        validate_logic_node(
            {
                "all": [
                    {"any": [{"field_present": "x"}, {"not": {"field_present": "y"}}]},
                    {
                        "for_each": {
                            "source": "ingredients.items",
                            "assert": {"field_present": "name"},
                        }
                    },
                ]
            }
        )

    @pytest.mark.parametrize(
        ("node", "path_fragment"),
        [
            ({}, "logic:"),  # empty mapping
            ({"a": 1, "b": 2}, "logic:"),  # more than one key
            ("not a mapping", "logic:"),
            ({"all": "not a list"}, "logic.all:"),
            ({"all": []}, "logic.all:"),  # empty list
            ({"for_each": {"source": "x"}}, "logic.for_each:"),  # missing 'assert'
            ({"for_each": {"assert": {"field_present": "x"}}}, "logic.for_each:"),  # missing source
            ({"for_each": "not a mapping"}, "logic.for_each:"),
            ({"Bad-Predicate-Name": "x"}, "logic.Bad-Predicate-Name:"),
        ],
    )
    def test_malformed_logic_nodes_raise_with_a_precise_path(
        self, node: object, path_fragment: str
    ) -> None:
        with pytest.raises(RuleParseError) as exc_info:
            validate_logic_node(node)
        assert path_fragment in str(exc_info.value)

    def test_malformed_logic_is_rejected_through_the_full_rule_model_too(self) -> None:
        with pytest.raises(ValidationError, match="logic"):
            Rule.model_validate(_rule(logic={"all": []}))


class TestPackManifest:
    def test_a_valid_manifest_parses(self) -> None:
        manifest = PackManifest.model_validate(VALID_MANIFEST)
        assert manifest.pack_id == "in-fssai-food"

    def test_manifest_is_frozen(self) -> None:
        manifest = PackManifest.model_validate(VALID_MANIFEST)
        with pytest.raises(ValidationError):
            manifest.version = "2.0.0"  # type: ignore[misc]

    @pytest.mark.parametrize(
        "pack_id", ["INFSSAIFOOD", "in_fssai_food", "in-fssai", "IN-FSSAI-FOOD"]
    )
    def test_malformed_pack_id_is_rejected(self, pack_id: str) -> None:
        with pytest.raises(ValidationError, match="pack_id"):
            PackManifest.model_validate(_manifest(pack_id=pack_id))

    @pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0-beta", "latest"])
    def test_non_semver_version_is_rejected(self, version: str) -> None:
        with pytest.raises(ValidationError, match="version"):
            PackManifest.model_validate(_manifest(version=version))

    def test_effective_to_before_effective_from_is_rejected(self) -> None:
        with pytest.raises(ValidationError, match="effective_to"):
            PackManifest.model_validate(
                _manifest(effective_from="2024-06-01", effective_to="2024-01-01")
            )

    def test_empty_source_citations_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            PackManifest.model_validate(_manifest(source_citations=[]))

    def test_jurisdiction_and_category_are_not_restricted_to_a_fixed_set(self) -> None:
        # Deliberately no allowlist here (see the module docstring): adding
        # a new jurisdiction/category must never require a code change.
        manifest = PackManifest.model_validate(
            _manifest(pack_id="us-fda-cosmetics", jurisdiction="US", category="cosmetics")
        )
        assert manifest.jurisdiction == "US"
        assert manifest.category == "cosmetics"
