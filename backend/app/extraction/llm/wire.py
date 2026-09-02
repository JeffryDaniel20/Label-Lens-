"""The LLM-facing output contract (P3-T5).

Deliberately **not** `LabelFacts`. That schema (P3-T4) is the frozen
AI↔rules contract and, by its own docstring, excludes per-field confidence
and evidence because "those live in the `extracted_fields` and
`evidence_spans` tables." What the model returns therefore needs a richer,
flatter shape, which this module defines, and
`app/extraction/service.py` maps deterministically into `LabelFacts` plus
per-field citation rows.

Three reasons this is a separate schema rather than reusing `LabelFacts`:

1. **Citations.** IMPLEMENTATION.md §8 step 5 requires every field to cite
   the OCR token ids it came from. `LabelFacts` has nowhere to put them.
2. **Structure comes from Python, not the model.** §8 step 7 is explicit
   that normalization - ingredient splitting, unit conversion, date parsing
   - is "deterministic Python, not the LLM". So the model returns text
   *as printed* and `app/extraction/normalize/` (P3-T7) does the parsing.
   The one exception is the nutrition table, whose row structure is a
   reading task rather than a parsing task and has no deterministic
   normalizer.
3. **`Fact[T]`'s invariant is a validator, not JSON Schema.** "Exactly one
   of value/not_found_reason" cannot be expressed in the schema a provider
   is given, so a model could satisfy the schema and still violate the
   invariant. Keeping the wire shape flat and mapping it in Python means
   that invariant is enforced where it can actually be enforced.

Token ids are small integers - indices into the token list rendered in the
prompt - not database UUIDs. A model asked to echo 36-character UUIDs
wastes output tokens and mistypes them; `service.py` maps indices back to
real `OcrTokenRow` ids.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

AddressRole = Literal["manufacturer", "packer", "marketer", "importer"]


class TextField(BaseModel):
    """One as-printed value, or an explicit reasoned absence."""

    value: str | None = Field(
        default=None,
        description="The text exactly as printed on the label. Null if not present.",
    )
    not_found_reason: str | None = Field(
        default=None,
        description=(
            "Required when value is null: a short factual reason, e.g. "
            "'no ingredient statement appears on the supplied pages'. "
            "Never guess a value instead of giving a reason."
        ),
    )
    token_ids: list[int] = Field(
        default_factory=list,
        description="Indices of the OCR tokens this value was read from. Empty if not found.",
    )
    confidence: float = Field(
        default=0.0,
        description="0.0-1.0 confidence that this value was read correctly.",
    )


class StringListField(BaseModel):
    values: list[str] = Field(default_factory=list)
    not_found_reason: str | None = None
    token_ids: list[int] = Field(default_factory=list)
    confidence: float = 0.0


class NutritionRowOut(BaseModel):
    nutrient: str = Field(description="Nutrient name exactly as printed, e.g. 'Total Fat'.")
    unit: str = Field(description="Unit exactly as printed, e.g. 'g', 'mg', 'kcal'.")
    per_100g: float | None = None
    per_serving: float | None = None
    token_ids: list[int] = Field(default_factory=list)


class ClaimOut(BaseModel):
    text: str = Field(description="The claim exactly as printed, e.g. 'High in Protein'.")
    token_ids: list[int] = Field(default_factory=list)


class AddressOut(BaseModel):
    role: AddressRole
    text: str = Field(description="The full address block exactly as printed.")
    token_ids: list[int] = Field(default_factory=list)


class ExtractionEnvelope(BaseModel):
    """Everything one extraction call returns."""

    ingredients_declared_text: TextField
    allergens_declaration_text: TextField
    allergens_declared: StringListField
    nutrition_serving_size: TextField
    nutrition_rows: list[NutritionRowOut] = Field(default_factory=list)
    nutrition_rows_not_found_reason: str | None = None
    quantity_net_quantity: TextField
    dates_manufacture: TextField
    dates_expiry_or_best_before: TextField
    dates_batch_number: TextField
    claims: list[ClaimOut] = Field(default_factory=list)
    claims_not_found_reason: str | None = None
    addresses: list[AddressOut] = Field(default_factory=list)
    addresses_not_found_reason: str | None = None
    languages_detected: StringListField
