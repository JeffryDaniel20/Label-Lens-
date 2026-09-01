"""Ruleset version diffing (P4-T4's "version diff view").

Pure function over two rule collections - no DB, no I/O - so a diff can be
computed either from two freshly-loaded `RulePack`s or from two `Ruleset`s
already reconstructed from the database via `app.rules.publish.load_ruleset`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.rules.schema import Rule

ChangeKind = Literal["added", "removed", "changed", "unchanged"]


@dataclass(slots=True, frozen=True)
class RuleDiffEntry:
    rule_key: str
    change: ChangeKind
    old_version: int | None
    new_version: int | None


def diff_rules(old_rules: list[Rule], new_rules: list[Rule]) -> list[RuleDiffEntry]:
    """One `RuleDiffEntry` per distinct `rule_key` across both collections,
    sorted for a deterministic, reviewable diff view."""
    old_by_key = {rule.rule_key: rule for rule in old_rules}
    new_by_key = {rule.rule_key: rule for rule in new_rules}

    entries: list[RuleDiffEntry] = []
    for key in sorted(old_by_key.keys() | new_by_key.keys()):
        old_rule, new_rule = old_by_key.get(key), new_by_key.get(key)
        if old_rule is None and new_rule is not None:
            entries.append(RuleDiffEntry(key, "added", None, new_rule.version))
        elif old_rule is not None and new_rule is None:
            entries.append(RuleDiffEntry(key, "removed", old_rule.version, None))
        elif old_rule is not None and new_rule is not None:
            change: ChangeKind = (
                "unchanged" if old_rule.model_dump() == new_rule.model_dump() else "changed"
            )
            entries.append(RuleDiffEntry(key, change, old_rule.version, new_rule.version))
    return entries
