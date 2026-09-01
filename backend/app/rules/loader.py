"""Rule pack loading and checksumming (P4-T1).

A pack is a `PackManifest` plus its `Rule`s. Loading either succeeds with a
fully validated, checksummed `RulePack`, or raises `PackValidationError`
listing *every* problem found (not just the first) - an invalid pack is
rejected outright, so nothing downstream (a publish command, an analysis)
can ever run against a partially-broken pack.

The checksum is computed over the validated, canonically-serialized
structure (sorted keys, no incidental whitespace), not the raw source
bytes - so two YAML files that differ only in comments, key order, or
formatting produce the *same* checksum, while any actual content change
produces a different one. That is what "checksum stability" means here.

A rule that parses structurally (P4-T1) but calls a predicate that doesn't
exist is still an invalid pack - `load_pack()` also runs P4-T2's
`validate_predicates_registered()` on every rule, so "an invalid pack cannot
be published" already covers that case here, not just at evaluation time.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.rules.predicates import UnregisteredPredicateError, validate_predicates_registered
from app.rules.schema import PackManifest, Rule


class PackValidationError(ValueError):
    """A pack (manifest + rules) failed validation. `issues` lists every
    problem found, not just the first."""

    def __init__(self, issues: list[str]) -> None:
        self.issues = issues
        super().__init__("; ".join(issues))


@dataclass(slots=True, frozen=True)
class RulePack:
    manifest: PackManifest
    rules: tuple[Rule, ...]
    checksum: str


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_pack_checksum(manifest: PackManifest, rules: list[Rule]) -> str:
    """A sha256 hex digest over the validated manifest + rules, stable
    across re-serialization and rule ordering, sensitive to any real
    content change."""
    sorted_rules = sorted(rules, key=lambda rule: rule.rule_key)
    payload = {
        "manifest": manifest.model_dump(mode="json"),
        "rules": [rule.model_dump(mode="json") for rule in sorted_rules],
    }
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _validation_error_messages(prefix: str, exc: ValidationError) -> list[str]:
    messages = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        messages.append(f"{prefix}{f'.{location}' if location else ''}: {error['msg']}")
    return messages


def load_pack(manifest_data: dict[str, Any], rules_data: list[dict[str, Any]]) -> RulePack:
    """Validate and assemble a `RulePack` from already-parsed manifest and
    rule dictionaries (e.g. from `yaml.safe_load`). Raises
    `PackValidationError` aggregating every problem found."""
    issues: list[str] = []

    manifest: PackManifest | None = None
    try:
        manifest = PackManifest.model_validate(manifest_data)
    except ValidationError as exc:
        issues.extend(_validation_error_messages("manifest", exc))

    rules: list[Rule] = []
    seen_keys: dict[str, int] = {}
    for index, rule_data in enumerate(rules_data):
        try:
            rule = Rule.model_validate(rule_data)
        except ValidationError as exc:
            key_hint = f"#{index}"
            if isinstance(rule_data, dict) and rule_data.get("rule_key"):
                key_hint = str(rule_data["rule_key"])
            issues.extend(_validation_error_messages(f"rules[{key_hint}]", exc))
            continue
        if rule.rule_key in seen_keys:
            issues.append(
                f"rules: duplicate rule_key {rule.rule_key!r} "
                f"(first at index {seen_keys[rule.rule_key]}, again at index {index})."
            )
            continue
        try:
            validate_predicates_registered(rule)
        except UnregisteredPredicateError as exc:
            issues.append(f"rules[{rule.rule_key}]: {exc}")
            continue
        seen_keys[rule.rule_key] = index
        rules.append(rule)

    if not rules_data:
        issues.append("rules: a pack must contain at least one rule.")

    if issues or manifest is None:
        raise PackValidationError(issues or ["manifest: failed to validate."])

    checksum = compute_pack_checksum(manifest, rules)
    return RulePack(manifest=manifest, rules=tuple(rules), checksum=checksum)


def load_pack_from_directory(path: Path) -> RulePack:
    """Load a pack from disk: `{path}/manifest.yaml` plus every
    `{path}/rules/*.yaml` (one rule per file, sorted by filename for a
    deterministic load order)."""
    manifest_path = path / "manifest.yaml"
    if not manifest_path.is_file():
        raise PackValidationError([f"manifest: {manifest_path} does not exist."])
    manifest_data = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))

    rules_dir = path / "rules"
    rule_files = sorted(rules_dir.glob("*.yaml")) if rules_dir.is_dir() else []
    rules_data = [yaml.safe_load(f.read_text(encoding="utf-8")) for f in rule_files]

    return load_pack(manifest_data, rules_data)
