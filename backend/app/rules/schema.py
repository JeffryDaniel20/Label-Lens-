"""The rule DSL schema (P4-T1): what a single rule and a pack manifest look
like, and strict structural validation of both.

This file has no dependencies on other LabelLens modules (matching the
module table in IMPLEMENTATION.md section 4: `rules | ... | -- (pure)`) -
only Pydantic and the standard library. `app.rules.predicates` (P4-T2) takes
the same stance for anything with I/O or ambient state, but accepts small
pieces of externally-owned *data* (e.g. an allergen dictionary) as explicit
predicate arguments rather than importing `extraction` directly - see that
module's docstring for why. Either way, the rule engine stays testable and
reasoned about in complete isolation from the AI pipeline that produces the
facts it will eventually evaluate.

`jurisdiction` and `category` are validated as well-formed strings, not
checked against a fixed allowlist: IMPLEMENTATION.md section 10 is explicit
that "adding a jurisdiction = new pack + dictionaries + fixtures ... no
schema change, no code rewrite" - hardcoding a jurisdiction/category enum
here would violate that directly and force a code change for every new
market. (Contrast `app.classification.classifier`, which *does* hardcode a
small recognized set - that module's job is to decide what to trust from
noisy AI output, a genuinely different problem from this one: validating the
shape of hand-authored regulatory content.)

This module defines *structure*, not semantics: it validates that a rule's
`logic` tree is well-formed (single-key mapping nodes, recognized
combinators with the right shape), but it does not know which predicate
names actually exist - that closed registry is P4-T2's job, layered on top
of a successfully parsed `Rule`.
"""

from __future__ import annotations

import enum
import re
from datetime import date
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_RULE_KEY_RE = re.compile(r"^[A-Z0-9]+(-[A-Z0-9]+)+$")
_PACK_ID_RE = re.compile(r"^[a-z0-9]+-[a-z0-9]+-[a-z0-9]+$")
_PREDICATE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_FIELD_PATH_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)*$")

_COMBINATORS = frozenset({"all", "any", "not", "for_each"})


class RuleParseError(ValueError):
    """A single rule failed schema validation."""


class Severity(enum.StrEnum):
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    ADVISORY = "advisory"


def _validate_field_path(path: str, where: str) -> str:
    if not isinstance(path, str) or not _FIELD_PATH_RE.match(path):
        raise RuleParseError(
            f"{where}: {path!r} is not a valid dotted field path "
            "(expected lower_snake_case segments joined by '.')."
        )
    return path


def validate_logic_node(node: Any, path: str = "logic") -> None:
    """Recursively validate a logic-tree node's *structure*.

    Raises `RuleParseError` with the exact node path on the first problem
    found, so a malformed rule fails with a precise, actionable location
    rather than a generic "invalid rule" message.
    """
    if not isinstance(node, dict) or len(node) != 1:
        raise RuleParseError(
            f"{path}: a logic node must be a mapping with exactly one key, got {node!r}."
        )
    (key, value), = node.items()

    if key in ("all", "any"):
        if not isinstance(value, list) or not value:
            raise RuleParseError(f"{path}.{key}: must be a non-empty list of logic nodes.")
        for index, child in enumerate(value):
            validate_logic_node(child, f"{path}.{key}[{index}]")
        return

    if key == "not":
        validate_logic_node(value, f"{path}.not")
        return

    if key == "for_each":
        if not isinstance(value, dict):
            raise RuleParseError(f"{path}.for_each: must be a mapping.")
        missing = {"source", "assert"} - value.keys()
        if missing:
            raise RuleParseError(
                f"{path}.for_each: missing required key(s) {sorted(missing)}."
            )
        _validate_field_path(value["source"], f"{path}.for_each.source")
        if "where" in value:
            validate_logic_node(value["where"], f"{path}.for_each.where")
        validate_logic_node(value["assert"], f"{path}.for_each.assert")
        return

    # A leaf predicate call: {predicate_name: <argument>}. The argument's
    # shape is the predicate's own business (P4-T2); only the name's syntax
    # is checked here.
    if not _PREDICATE_NAME_RE.match(key):
        raise RuleParseError(
            f"{path}.{key}: {key!r} is not a valid predicate name "
            "(expected lower_snake_case)."
        )


class RuleApplicability(BaseModel):
    model_config = ConfigDict(frozen=True)

    jurisdiction: list[str] = Field(min_length=1)
    category: list[str] = Field(min_length=1)
    predicates: list[dict[str, Any]] = Field(default_factory=list)

    @field_validator("predicates")
    @classmethod
    def _validate_predicates(cls, value: list[dict[str, Any]]) -> list[dict[str, Any]]:
        for index, predicate in enumerate(value):
            validate_logic_node(predicate, f"applicability.predicates[{index}]")
        return value


class RuleMessages(BaseModel):
    model_config = ConfigDict(frozen=True, populate_by_name=True)

    pass_: str | None = Field(default=None, alias="pass")
    fail: str | None = None
    warn: str | None = None
    insufficient_data: str


class RuleEvidence(BaseModel):
    model_config = ConfigDict(frozen=True)

    fields: list[str] = Field(min_length=1)

    @field_validator("fields")
    @classmethod
    def _validate_fields(cls, value: list[str]) -> list[str]:
        for field_path in value:
            _validate_field_path(field_path, "evidence.fields")
        return value


class Rule(BaseModel):
    model_config = ConfigDict(frozen=True)

    rule_key: str
    version: int = Field(ge=1)
    title: str = Field(min_length=1)
    citation: str = Field(min_length=1)
    severity: Severity
    effective_from: date
    effective_to: date | None = None
    applicability: RuleApplicability
    logic: dict[str, Any]
    requires_fields: list[str] = Field(default_factory=list)
    # "insufficient_data ≠ pass" is an engine-level guarantee (P4-T3),
    # enforced regardless of what any single rule says - but the DSL itself
    # cannot even claim otherwise: this is the only value it accepts here.
    on_missing_fields: Literal["insufficient_data"]
    message: RuleMessages
    evidence: RuleEvidence

    @field_validator("rule_key")
    @classmethod
    def _validate_rule_key(cls, value: str) -> str:
        if not _RULE_KEY_RE.match(value):
            raise RuleParseError(
                f"rule_key {value!r} must be upper-snake segments joined by '-' "
                "(e.g. 'IN-FSSAI-FOOD-ALLERGEN-DECL')."
            )
        return value

    @field_validator("requires_fields")
    @classmethod
    def _validate_requires_fields(cls, value: list[str]) -> list[str]:
        for field_path in value:
            _validate_field_path(field_path, "requires_fields")
        return value

    @field_validator("logic")
    @classmethod
    def _validate_logic(cls, value: dict[str, Any]) -> dict[str, Any]:
        validate_logic_node(value, "logic")
        return value

    @model_validator(mode="after")
    def _effective_window_is_ordered(self) -> Rule:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise RuleParseError(
                f"{self.rule_key}: effective_to ({self.effective_to}) is before "
                f"effective_from ({self.effective_from})."
            )
        return self


class PackManifest(BaseModel):
    model_config = ConfigDict(frozen=True)

    pack_id: str
    jurisdiction: str = Field(min_length=1)
    category: str = Field(min_length=1)
    version: str
    effective_from: date
    effective_to: date | None = None
    source_citations: list[str] = Field(min_length=1)
    author: str = Field(min_length=1)
    review_date: date

    @field_validator("pack_id")
    @classmethod
    def _validate_pack_id(cls, value: str) -> str:
        if not _PACK_ID_RE.match(value):
            raise RuleParseError(
                f"pack_id {value!r} must be '{{jurisdiction}}-{{authority}}-{{category}}' "
                "in lowercase (e.g. 'in-fssai-food')."
            )
        return value

    @field_validator("version")
    @classmethod
    def _validate_semver(cls, value: str) -> str:
        if not re.match(r"^\d+\.\d+\.\d+$", value):
            raise RuleParseError(f"version {value!r} must be a semver string 'X.Y.Z'.")
        return value

    @model_validator(mode="after")
    def _effective_window_is_ordered(self) -> PackManifest:
        if self.effective_to is not None and self.effective_to < self.effective_from:
            raise RuleParseError(
                f"{self.pack_id}: effective_to ({self.effective_to}) is before "
                f"effective_from ({self.effective_from})."
            )
        return self
