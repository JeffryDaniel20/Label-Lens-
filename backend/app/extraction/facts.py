"""The fact schema: the frozen contract between AI and rules (P3-T4).

`LabelFacts` is what OCR + LLM extraction produce and what the (not yet
built) rule engine consumes - nothing upstream of this schema ever emits a
compliance verdict, and nothing downstream of it ever looks at raw pixels or
prompts again. That separation is the "AI extracts, rules decide" principle
this whole project is built around.

Every leaf value is wrapped in `Fact[T]`, which enforces the one invariant
this schema exists to guarantee: a field is either a real, present value, or
`None` with an explicit `not_found_reason` - there is no way to construct a
"quietly missing" field. An extractor that cannot read the net quantity must
say so; it may not simply omit the value and let a rule silently treat it as
absent-therefore-compliant. This is the schema-level backstop for
IMPLEMENTATION.md's "fabrication bait" cases (an empty label, a blank page, a
photo of a cat) - even when every single fact is `not_found`, `LabelFacts`
still validates, and every reason is recorded.

Per-field confidence and evidence (which OCR tokens a value came from) are
deliberately *not* part of this schema - those live in the `extracted_fields`
and `evidence_spans` tables (P3-T5/P3-T6), keyed by the same dotted field
path (e.g. `ingredients.items`) that `rules/*.yaml` already references. This
file only defines the shape of the data itself.

`SCHEMA_VERSION` is bumped on any breaking change to this contract; an
`extraction` row records the version of the schema its payload validates
against, so an old analysis can always be re-read against the schema it was
actually produced under.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

SCHEMA_VERSION: Literal["1.0.0"] = "1.0.0"


class Fact[T](BaseModel):
    """A single extracted value that is either present or explicitly absent.

    Exactly one of `value` / `not_found_reason` must be set - never both,
    never neither. This is the schema's core anti-hallucination guarantee:
    it is structurally impossible to represent "we don't know" as a value
    (e.g. an empty string or a made-up default) instead of as an explicit,
    reasoned absence.
    """

    model_config = ConfigDict(frozen=True)

    value: T | None = None
    not_found_reason: str | None = None

    @model_validator(mode="after")
    def _exactly_one_of_value_or_reason(self) -> Fact[T]:
        if self.value is None and self.not_found_reason is None:
            raise ValueError(
                "A Fact with no value must carry a not_found_reason; "
                "a field may never be silently missing."
            )
        if self.value is not None and self.not_found_reason is not None:
            raise ValueError(
                "A Fact with a value must not also carry a not_found_reason "
                "(they are mutually exclusive)."
            )
        return self

    @classmethod
    def found(cls, value: T) -> Fact[T]:
        return cls(value=value)

    @classmethod
    def missing(cls, reason: str) -> Fact[T]:
        return cls(not_found_reason=reason)


class IngredientItem(BaseModel):
    """One entry in the declared ingredient list, in declared order."""

    name: str
    position: int
    percentage: float | None = None  # a QUID declaration, when present


class IngredientsFacts(BaseModel):
    # The ingredient statement as printed, before P3-T7 splits it into items.
    declared_text: Fact[str]
    items: Fact[list[IngredientItem]]


class AllergensFacts(BaseModel):
    # The allergen statement as printed (e.g. "Contains: Milk, Soy").
    declaration_text: Fact[str]
    # The allergens it explicitly names, one string per allergen as declared.
    declared: Fact[list[str]]


class NutritionRow(BaseModel):
    """One line of the nutrition table, values as printed (pre-normalization)."""

    nutrient: str
    unit: str
    per_100g: float | None = None
    per_serving: float | None = None


class NutritionFacts(BaseModel):
    serving_size: Fact[str]
    rows: Fact[list[NutritionRow]]


class QuantityFacts(BaseModel):
    # The net-quantity declaration as printed (e.g. "250 g", "1 L"); P3-T7
    # is responsible for parsing this into a normalized value + unit.
    net_quantity: Fact[str]


class DatesFacts(BaseModel):
    manufacture_date: Fact[str]
    expiry_or_best_before: Fact[str]
    batch_number: Fact[str]


class Claim(BaseModel):
    text: str


class ClaimsFacts(BaseModel):
    items: Fact[list[Claim]]


class Address(BaseModel):
    role: Literal["manufacturer", "packer", "marketer", "importer"]
    text: str


class AddressesFacts(BaseModel):
    items: Fact[list[Address]]


class LanguagesFacts(BaseModel):
    # ISO 639-1 codes for every language detected anywhere on the label.
    detected: Fact[list[str]]


class LabelFacts(BaseModel):
    """The complete normalized fact set for one product version's label."""

    model_config = ConfigDict(frozen=True)

    schema_version: Literal["1.0.0"] = SCHEMA_VERSION
    ingredients: IngredientsFacts
    allergens: AllergensFacts
    nutrition: NutritionFacts
    quantity: QuantityFacts
    dates: DatesFacts
    claims: ClaimsFacts
    addresses: AddressesFacts
    languages: LanguagesFacts
