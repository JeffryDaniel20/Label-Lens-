"""Extraction orchestration (P3-T5).

Owns everything that must behave identically regardless of which provider is
configured: prompt assembly, schema validation, the single repair retry,
model-tier escalation, citation resolution, and the deterministic mapping
from the wire envelope onto `LabelFacts`.

The acceptance criterion this file exists to satisfy - "extraction returns a
valid fact object with citations, or fails explicitly, never partially
valid" - is structural rather than a matter of care: nothing is persisted
until a fully validated envelope exists, so a run that exhausts its retries
raises `ExtractionFailed` and writes no `Extraction` row at all. There is no
code path that saves half an extraction.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog.models import File, FilePage
from app.db.session import tenant_scoped
from app.extraction import facts as facts_schema
from app.extraction.llm import base as provider_base
from app.extraction.llm import prompt as prompt_module
from app.extraction.llm.wire import ExtractionEnvelope
from app.extraction.models import ExtractedField, Extraction
from app.vision.models import OcrTokenRow

MAX_ATTEMPTS = 3


class ExtractionFailed(Exception):
    """Extraction could not produce a valid fact object.

    Deliberately distinct from `ProviderError`: this means the provider
    answered but never satisfied the schema, which is an extraction-quality
    failure (route to human review) rather than an infrastructure one.
    """


@dataclass(slots=True, frozen=True)
class ExtractionOutcome:
    extraction: Extraction
    label_facts: facts_schema.LabelFacts
    tokens_in: int
    tokens_out: int


def load_ocr_tokens(
    db: Session, *, organization_id: uuid.UUID, product_version_id: uuid.UUID
) -> list[OcrTokenRow]:
    """Every OCR token for a product version's pages, in reading order.

    Ordered page-by-page then line-by-line then left-to-right, so the token
    indices the model sees follow the label as a human would read it -
    adjacency carries real meaning for panel detection, and a shuffled list
    would quietly make extraction harder for no reason.
    """
    stmt = (
        tenant_scoped(
            select(OcrTokenRow)
            .join(FilePage, FilePage.id == OcrTokenRow.file_page_id)
            .join(File, File.id == FilePage.file_id)
            .where(File.product_version_id == product_version_id),
            OcrTokenRow,
            organization_id,
        )
        .order_by(FilePage.file_id, FilePage.page_no, OcrTokenRow.line_no, OcrTokenRow.x1)
    )
    return list(db.scalars(stmt).all())


def _resolve_citations(
    token_ids: Sequence[int], tokens: Sequence[OcrTokenRow]
) -> list[str]:
    """Map model-supplied token indices back to real `ocr_tokens.id`s.

    An out-of-range index is dropped rather than raising: a fabricated
    citation is exactly the kind of thing the P3-T6 evidence gate exists to
    catch, and a value that ends up with no resolvable citation will be
    demoted there. Crashing the whole extraction over one bad index would
    lose the other fields that were read correctly.
    """
    resolved: list[str] = []
    for index in token_ids:
        if 0 <= index < len(tokens):
            resolved.append(str(tokens[index].id))
    return resolved


def _fact_from_text(field: object) -> facts_schema.Fact[str]:
    value = getattr(field, "value", None)
    if value:
        return facts_schema.Fact.found(value)
    reason = getattr(field, "not_found_reason", None) or "not reported by the extractor"
    return facts_schema.Fact.missing(reason)


def _fact_from_list[T](values: Sequence[T], reason: str | None) -> facts_schema.Fact[list[T]]:
    if values:
        return facts_schema.Fact.found(list(values))
    return facts_schema.Fact.missing(reason or "not reported by the extractor")


def envelope_to_label_facts(envelope: ExtractionEnvelope) -> facts_schema.LabelFacts:
    """Deterministic wire→contract mapping. No model involvement.

    `ingredients.items` is recorded as an explicit, reasoned absence rather
    than parsed here: IMPLEMENTATION.md §8 step 7 assigns ingredient
    splitting to deterministic Python in the *normalizing* stage (P3-T7), so
    at extraction time those items genuinely do not exist yet. `Fact`'s
    value-or-reason invariant makes that state representable honestly
    instead of as a silently empty list.
    """
    pending = "pending normalization (populated by the normalizing stage)"
    return facts_schema.LabelFacts(
        ingredients=facts_schema.IngredientsFacts(
            declared_text=_fact_from_text(envelope.ingredients_declared_text),
            items=facts_schema.Fact.missing(pending),
        ),
        allergens=facts_schema.AllergensFacts(
            declaration_text=_fact_from_text(envelope.allergens_declaration_text),
            declared=_fact_from_list(
                envelope.allergens_declared.values,
                envelope.allergens_declared.not_found_reason,
            ),
        ),
        nutrition=facts_schema.NutritionFacts(
            serving_size=_fact_from_text(envelope.nutrition_serving_size),
            rows=_fact_from_list(
                [
                    facts_schema.NutritionRow(
                        nutrient=row.nutrient,
                        unit=row.unit,
                        per_100g=row.per_100g,
                        per_serving=row.per_serving,
                    )
                    for row in envelope.nutrition_rows
                ],
                envelope.nutrition_rows_not_found_reason,
            ),
        ),
        quantity=facts_schema.QuantityFacts(
            net_quantity=_fact_from_text(envelope.quantity_net_quantity),
        ),
        dates=facts_schema.DatesFacts(
            manufacture_date=_fact_from_text(envelope.dates_manufacture),
            expiry_or_best_before=_fact_from_text(envelope.dates_expiry_or_best_before),
            batch_number=_fact_from_text(envelope.dates_batch_number),
        ),
        claims=facts_schema.ClaimsFacts(
            items=_fact_from_list(
                [facts_schema.Claim(text=c.text) for c in envelope.claims],
                envelope.claims_not_found_reason,
            ),
        ),
        addresses=facts_schema.AddressesFacts(
            items=_fact_from_list(
                [facts_schema.Address(role=a.role, text=a.text) for a in envelope.addresses],
                envelope.addresses_not_found_reason,
            ),
        ),
        languages=facts_schema.LanguagesFacts(
            detected=_fact_from_list(
                envelope.languages_detected.values,
                envelope.languages_detected.not_found_reason,
            ),
        ),
    )


def _field_rows(
    envelope: ExtractionEnvelope, tokens: Sequence[OcrTokenRow]
) -> list[dict[str, object]]:
    """One record per dotted field path, carrying its resolved citations."""

    def text_row(path: str, field: object) -> dict[str, object]:
        return {
            "field_path": path,
            "value_raw": getattr(field, "value", None),
            "not_found_reason": getattr(field, "not_found_reason", None),
            "confidence": float(getattr(field, "confidence", 0.0) or 0.0),
            "cited_token_ids": _resolve_citations(
                getattr(field, "token_ids", []) or [], tokens
            ),
        }

    def joined_row(
        path: str, values: Sequence[str], reason: str | None, cited: Sequence[int], conf: float
    ) -> dict[str, object]:
        return {
            "field_path": path,
            "value_raw": "; ".join(values) if values else None,
            "not_found_reason": reason if not values else None,
            "confidence": conf,
            "cited_token_ids": _resolve_citations(cited, tokens),
        }

    rows: list[dict[str, object]] = [
        text_row("ingredients.declared_text", envelope.ingredients_declared_text),
        text_row("allergens.declaration_text", envelope.allergens_declaration_text),
        joined_row(
            "allergens.declared",
            envelope.allergens_declared.values,
            envelope.allergens_declared.not_found_reason,
            envelope.allergens_declared.token_ids,
            envelope.allergens_declared.confidence,
        ),
        text_row("nutrition.serving_size", envelope.nutrition_serving_size),
        text_row("quantity.net_quantity", envelope.quantity_net_quantity),
        text_row("dates.manufacture_date", envelope.dates_manufacture),
        text_row("dates.expiry_or_best_before", envelope.dates_expiry_or_best_before),
        text_row("dates.batch_number", envelope.dates_batch_number),
        joined_row(
            "languages.detected",
            envelope.languages_detected.values,
            envelope.languages_detected.not_found_reason,
            envelope.languages_detected.token_ids,
            envelope.languages_detected.confidence,
        ),
    ]

    nutrition_citations = [i for row in envelope.nutrition_rows for i in row.token_ids]
    rows.append(
        joined_row(
            "nutrition.rows",
            [f"{r.nutrient} {r.per_100g if r.per_100g is not None else ''}{r.unit}".strip()
             for r in envelope.nutrition_rows],
            envelope.nutrition_rows_not_found_reason,
            nutrition_citations,
            1.0 if envelope.nutrition_rows else 0.0,
        )
    )
    rows.append(
        joined_row(
            "claims.items",
            [c.text for c in envelope.claims],
            envelope.claims_not_found_reason,
            [i for c in envelope.claims for i in c.token_ids],
            1.0 if envelope.claims else 0.0,
        )
    )
    rows.append(
        joined_row(
            "addresses.items",
            [f"{a.role}: {a.text}" for a in envelope.addresses],
            envelope.addresses_not_found_reason,
            [i for a in envelope.addresses for i in a.token_ids],
            1.0 if envelope.addresses else 0.0,
        )
    )
    return rows


def generate_envelope(
    provider: provider_base.ExtractionProvider,
    *,
    tokens: Sequence[OcrTokenRow],
    model: str,
    escalation_model: str,
) -> tuple[ExtractionEnvelope, provider_base.ProviderResponse, int, bool]:
    """Call the provider until a schema-valid envelope comes back.

    IMPLEMENTATION.md §8 step 6, exactly: parse; on failure one repair retry
    with the validation error fed back; on the second failure escalate the
    model tier; on the third give up. Returns
    `(envelope, last_response, attempts, escalated)`.
    """
    user_prompt = prompt_module.build_user_prompt(
        [prompt_module.RenderedToken(token_id=i, text=t.text) for i, t in enumerate(tokens)]
    )

    tokens_in = tokens_out = 0
    last_error = ""
    escalated = False
    current_prompt = user_prompt
    current_model = model

    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = provider.generate(
            system_instruction=prompt_module.SYSTEM_INSTRUCTION,
            prompt=current_prompt,
            schema=ExtractionEnvelope,
            model=current_model,
        )
        tokens_in += response.usage.tokens_in
        tokens_out += response.usage.tokens_out

        try:
            envelope = ExtractionEnvelope.model_validate_json(response.text)
        except ValidationError as exc:
            last_error = str(exc)
            # Next attempt: repair first, then escalate the tier.
            current_prompt = f"{user_prompt}\n\n{prompt_module.repair_instruction(last_error)}"
            if attempt >= 2:
                current_model = escalation_model
                escalated = True
            continue

        # Usage is accumulated across every attempt, so a repaired
        # extraction still reports what the failed attempts really cost.
        billed = provider_base.ProviderResponse(
            text=response.text,
            usage=provider_base.ProviderUsage(tokens_in=tokens_in, tokens_out=tokens_out),
            model=response.model,
        )
        return envelope, billed, attempt, escalated

    raise ExtractionFailed(
        f"No schema-valid extraction after {MAX_ATTEMPTS} attempts. "
        f"Last validation error: {last_error}"
    )


def extract_for_analysis(
    db: Session,
    *,
    provider: provider_base.ExtractionProvider,
    organization_id: uuid.UUID,
    analysis_id: uuid.UUID,
    product_version_id: uuid.UUID,
    model: str,
    escalation_model: str,
) -> ExtractionOutcome:
    """Run extraction for one analysis and persist the result.

    Nothing is written unless a fully validated envelope was produced first
    - the "never partially valid" half of the acceptance criterion.
    """
    tokens = load_ocr_tokens(
        db, organization_id=organization_id, product_version_id=product_version_id
    )
    if not tokens:
        raise ExtractionFailed(
            "No OCR tokens exist for this product version; nothing to extract from."
        )

    envelope, response, attempts, escalated = generate_envelope(
        provider, tokens=tokens, model=model, escalation_model=escalation_model
    )
    label_facts = envelope_to_label_facts(envelope)

    extraction = Extraction(
        organization_id=organization_id,
        analysis_id=analysis_id,
        schema_version=facts_schema.SCHEMA_VERSION,
        payload=label_facts.model_dump(mode="json"),
        envelope=envelope.model_dump(mode="json"),
        provider=provider.name,
        model=response.model,
        prompt_version=prompt_module.PROMPT_VERSION,
        prompt_hash=prompt_module.prompt_hash(),
        attempts=attempts,
        escalated=escalated,
        tokens_in=response.usage.tokens_in,
        tokens_out=response.usage.tokens_out,
    )
    db.add(extraction)
    db.flush()

    for row in _field_rows(envelope, tokens):
        db.add(
            ExtractedField(
                organization_id=organization_id,
                extraction_id=extraction.id,
                **row,
            )
        )
    db.flush()

    return ExtractionOutcome(
        extraction=extraction,
        label_facts=label_facts,
        tokens_in=response.usage.tokens_in,
        tokens_out=response.usage.tokens_out,
    )
