"""Evidence verification gate (P3-T6).

Matches every extracted value with a real value against the OCR tokens it
cited, demoting anything that doesn't match closely enough.
IMPLEMENTATION.md's own acceptance criterion - "hallucinated fields cannot
satisfy a rule" - is made structurally true here, not merely checked: a
demoted field's corresponding `LabelFacts` entry is rewritten to an explicit
`Fact.missing(...)` *before* the calling stage's own transition commits, so
there is no code path by which `rule_eval` (or anything else downstream)
could ever read a value that failed this check. The rule engine does not
need to know this gate exists - matching the "AI extracts, rules decide"
principle by construction, not by convention.

MATCHING ALGORITHM
-------------------
"Substring-matchable, fuzzy, ratio >= 0.85" (IMPLEMENTATION.md §8 step 6) is
a different question from "do these two strings match": the cited OCR text
is usually a whole line ("Net Quantity: 250 g"), not just the value itself
("250 g"), so a naive `SequenceMatcher(value, cited_text).ratio()` scores
low purely because of the surrounding text, not because the value is wrong.
`partial_ratio()` below finds the best-aligned equal-length window of the
longer string against the shorter one first (the same approach well-known
fuzzy-matching libraries call "partial ratio"), then scores that window.
No new dependency: `difflib.SequenceMatcher` is stdlib and deterministic,
matching this project's established preference for determinism over a
convenient library (see `app/extraction/normalize/language.py`'s choice of
`py3langid` over `langdetect` for exactly this reason).

SCOPE, STATED HONESTLY
----------------------
Verification runs at the same granularity `app/extraction/service.py`
already persists `ExtractedField` rows at - one row, and therefore one
verification, per dotted field path, even for list-shaped facts
(`nutrition.rows`, `claims.items`, `addresses.items`) that aggregate
multiple envelope items into one joined string. A single fabricated item
inside an otherwise-real list demotes the whole list rather than just that
item. Per-item verification would need per-item `ExtractedField` rows - a
real, larger redesign of P3-T5's own persistence granularity, not attempted
here.

ORCHESTRATION
-------------
This module is pure verification logic with no opinion about when it runs -
`app.analysis.stages._evidence_verification` is the actual orchestrator
stage that calls `verify_extraction`, positioned as its own state between
`extracting` and `normalizing` (P3-T6, wired as a real pipeline stage rather
than a side effect of extraction itself). `extract_for_analysis` no longer
calls this module at all.
"""

from __future__ import annotations

import difflib
import re
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.extraction import facts as facts_schema
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.vision.models import OcrTokenRow

MATCH_THRESHOLD = 0.85
EVIDENCE_SOURCE_OCR = "ocr"
DEMOTION_REASON = "the value could not be verified against its cited OCR tokens"

_WHITESPACE = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Case- and whitespace-insensitive: 'legitimate normalizations (case,
    spacing, unit)' (IMPLEMENTATION.md's own test line) must never cost a
    match, so both are folded away before any fuzzy comparison runs."""
    return _WHITESPACE.sub(" ", text).strip().casefold()


def partial_ratio(value: str, cited_text: str) -> float:
    """The best `SequenceMatcher` ratio between `value` and any
    equal-length window of `cited_text` aligned to it - see the module
    docstring for why a plain full-string ratio is the wrong question."""
    value = _normalize(value)
    cited_text = _normalize(cited_text)
    if not value or not cited_text:
        return 0.0

    short, long_ = (value, cited_text) if len(value) <= len(cited_text) else (cited_text, value)
    matcher = difflib.SequenceMatcher(None, short, long_)
    best = matcher.ratio()
    for block in matcher.get_matching_blocks():
        start = max(0, block.b - block.a)
        window = long_[start : start + len(short)]
        if window:
            best = max(best, difflib.SequenceMatcher(None, short, window).ratio())
    return best


@dataclass(slots=True, frozen=True)
class VerificationSummary:
    verified_count: int
    demoted_count: int


def _spans_multiple_pages(tokens: list[OcrTokenRow]) -> bool:
    """A field's citations are expected to come from one contiguous location
    on one page (`EvidenceSpan` itself only ever models a single page - see
    `verify_extraction`'s own comment on why the first token's page is used
    as *the* span's page). Citations resolving to more than one distinct
    `file_page_id` is exactly the kind of contradiction a hallucinating or
    confused model can produce (real evidence for one printed field does not
    legitimately span two separate label pages), so it is treated as an
    invalid citation - the same fail-safe outcome as no citation at all -
    rather than arbitrarily picking one page and silently discarding the
    other tokens' contribution to the match."""
    return len({t.file_page_id for t in tokens}) > 1


def _bbox_union(tokens: list[OcrTokenRow]) -> tuple[float, float, float, float]:
    return (
        min(t.x1 for t in tokens),
        min(t.y1 for t in tokens),
        max(t.x2 for t in tokens),
        max(t.y2 for t in tokens),
    )


def _demote(label_facts: facts_schema.LabelFacts, field_path: str) -> facts_schema.LabelFacts:
    """Rewrite the `LabelFacts` entry at `field_path` to an explicit,
    reasoned absence. One small case per dotted path (see the module
    docstring's scope note) rather than a generic path-setter, so every
    case is visible and typo-proof at a glance - and a path this module
    doesn't recognize fails loudly rather than silently doing nothing."""
    missing: facts_schema.Fact[Any] = facts_schema.Fact.missing(DEMOTION_REASON)

    if field_path == "ingredients.declared_text":
        return label_facts.model_copy(
            update={
                "ingredients": label_facts.ingredients.model_copy(
                    update={"declared_text": missing}
                )
            }
        )
    if field_path == "allergens.declaration_text":
        return label_facts.model_copy(
            update={
                "allergens": label_facts.allergens.model_copy(update={"declaration_text": missing})
            }
        )
    if field_path == "allergens.declared":
        return label_facts.model_copy(
            update={"allergens": label_facts.allergens.model_copy(update={"declared": missing})}
        )
    if field_path == "nutrition.serving_size":
        return label_facts.model_copy(
            update={
                "nutrition": label_facts.nutrition.model_copy(update={"serving_size": missing})
            }
        )
    if field_path == "nutrition.rows":
        return label_facts.model_copy(
            update={"nutrition": label_facts.nutrition.model_copy(update={"rows": missing})}
        )
    if field_path == "quantity.net_quantity":
        return label_facts.model_copy(
            update={"quantity": label_facts.quantity.model_copy(update={"net_quantity": missing})}
        )
    if field_path == "dates.manufacture_date":
        return label_facts.model_copy(
            update={"dates": label_facts.dates.model_copy(update={"manufacture_date": missing})}
        )
    if field_path == "dates.expiry_or_best_before":
        return label_facts.model_copy(
            update={
                "dates": label_facts.dates.model_copy(update={"expiry_or_best_before": missing})
            }
        )
    if field_path == "dates.batch_number":
        return label_facts.model_copy(
            update={"dates": label_facts.dates.model_copy(update={"batch_number": missing})}
        )
    if field_path == "claims.items":
        return label_facts.model_copy(
            update={"claims": label_facts.claims.model_copy(update={"items": missing})}
        )
    if field_path == "addresses.items":
        return label_facts.model_copy(
            update={"addresses": label_facts.addresses.model_copy(update={"items": missing})}
        )
    if field_path == "languages.detected":
        return label_facts.model_copy(
            update={"languages": label_facts.languages.model_copy(update={"detected": missing})}
        )
    raise ValueError(f"Unknown field_path for demotion: {field_path!r}")


def verify_extraction(
    db: Session, *, extraction: Extraction, label_facts: facts_schema.LabelFacts
) -> tuple[facts_schema.LabelFacts, VerificationSummary]:
    """Verifies every `ExtractedField` for `extraction`, demoting any whose
    value doesn't match its citations closely enough, and returns the
    (possibly-corrected) `LabelFacts` plus a count of each outcome -
    "demotions are counted as a metric" (IMPLEMENTATION.md's own acceptance
    line), tallied here and persisted onto `extraction` itself.

    Must run *before* `extraction.payload` is persisted, so a demotion is
    never merely advisory - the row `rule_eval` will eventually read back
    already reflects it.
    """
    fields = list(
        db.scalars(
            select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
        ).all()
    )
    verified_count = 0
    demoted_count = 0

    for field in fields:
        if field.value_raw is None:
            continue  # nothing to verify - an honest not_found already

        tokens: list[OcrTokenRow] = []
        if field.cited_token_ids:
            token_ids = [uuid.UUID(s) for s in field.cited_token_ids]
            tokens = list(
                db.scalars(select(OcrTokenRow).where(OcrTokenRow.id.in_(token_ids))).all()
            )

        if not tokens:
            # No citation at all, or every cited id failed to resolve to a
            # real token (already logged as a dropped hallucinated citation
            # in `app.extraction.service._resolve_citations`) - either way,
            # nothing to verify against, so it cannot pass.
            field.verified = False
            field.match_ratio = 0.0
            demoted_count += 1
            label_facts = _demote(label_facts, field.field_path)
            continue

        if _spans_multiple_pages(tokens):
            # A contradictory citation, not merely a missing one - real
            # evidence for one field does not legitimately span two pages.
            # Fails exactly the same way as no citation: never partial
            # credit for the tokens that happen to be on one page.
            field.verified = False
            field.match_ratio = 0.0
            demoted_count += 1
            label_facts = _demote(label_facts, field.field_path)
            continue

        cited_text = " ".join(t.text for t in tokens)
        ratio = partial_ratio(field.value_raw, cited_text)
        field.match_ratio = ratio
        field.verified = ratio >= MATCH_THRESHOLD

        if not field.verified:
            demoted_count += 1
            label_facts = _demote(label_facts, field.field_path)
            continue

        verified_count += 1
        x1, y1, x2, y2 = _bbox_union(tokens)
        db.add(
            EvidenceSpan(
                organization_id=field.organization_id,
                extracted_field_id=field.id,
                # A cross-page citation was already rejected above, so every
                # remaining token here shares one `file_page_id` - safe to
                # take the first rather than modelling a cross-page span.
                file_page_id=tokens[0].file_page_id,
                token_ids=list(field.cited_token_ids),
                x1=x1,
                y1=y1,
                x2=x2,
                y2=y2,
                text_snippet=cited_text[:2000],
                source=EVIDENCE_SOURCE_OCR,
            )
        )

    extraction.verified_field_count = verified_count
    extraction.demoted_field_count = demoted_count
    db.flush()
    return label_facts, VerificationSummary(
        verified_count=verified_count, demoted_count=demoted_count
    )
