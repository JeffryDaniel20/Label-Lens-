"""Unit tests for ruleset version diffing (P4-T4's "version diff view").

Pure function, no DB, no live services.
"""

from __future__ import annotations

import pytest

from app.rules.diff import diff_rules
from app.rules.schema import Rule

pytestmark = pytest.mark.unit


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "rule_key": "IN-TEST-A",
        "version": 1,
        "title": "t",
        "citation": "c",
        "severity": "major",
        "effective_from": "2020-01-01",
        "applicability": {"jurisdiction": ["IN"], "category": ["packaged_food"], "predicates": []},
        "logic": {"field_present": "x"},
        "requires_fields": [],
        "on_missing_fields": "insufficient_data",
        "message": {"insufficient_data": "y"},
        "evidence": {"fields": ["x"]},
    }
    base.update(overrides)
    return Rule.model_validate(base)


class TestDiffRules:
    def test_a_new_rule_is_added(self) -> None:
        entries = diff_rules([], [_rule()])
        assert len(entries) == 1
        assert entries[0].change == "added"
        assert entries[0].old_version is None
        assert entries[0].new_version == 1

    def test_a_removed_rule_key_is_removed(self) -> None:
        entries = diff_rules([_rule()], [])
        assert entries[0].change == "removed"
        assert entries[0].new_version is None

    def test_an_identical_rule_is_unchanged(self) -> None:
        entries = diff_rules([_rule()], [_rule()])
        assert entries[0].change == "unchanged"
        assert entries[0].old_version == entries[0].new_version == 1

    def test_a_bumped_version_with_different_content_is_changed(self) -> None:
        old = _rule(version=1)
        new = _rule(version=2, title="a new title")
        entries = diff_rules([old], [new])
        assert entries[0].change == "changed"
        assert entries[0].old_version == 1
        assert entries[0].new_version == 2

    def test_entries_are_sorted_by_rule_key_for_a_deterministic_diff(self) -> None:
        rule_b = _rule(rule_key="IN-TEST-B")
        rule_a = _rule(rule_key="IN-TEST-A")
        entries = diff_rules([rule_b], [rule_a, rule_b])
        assert [e.rule_key for e in entries] == ["IN-TEST-A", "IN-TEST-B"]

    def test_a_full_realistic_diff(self) -> None:
        old_rules = [_rule(rule_key="IN-TEST-A"), _rule(rule_key="IN-TEST-B")]
        new_rules = [
            _rule(rule_key="IN-TEST-A"),  # unchanged
            _rule(rule_key="IN-TEST-B", version=2, title="changed"),  # changed
            _rule(rule_key="IN-TEST-C"),  # added
        ]
        entries = diff_rules(old_rules, new_rules)
        by_key = {e.rule_key: e.change for e in entries}
        assert by_key == {
            "IN-TEST-A": "unchanged",
            "IN-TEST-B": "changed",
            "IN-TEST-C": "added",
        }
