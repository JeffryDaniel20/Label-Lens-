"""Confidence model and tier routing (P3-T8): combine OCR, extraction,
evidence-verification, AND classification (P3-T9) confidences into a
per-field confidence, then roll those up into one analysis-level tier -
IMPLEMENTATION.md §14's routing table, taken literally. §30's own objective
line for this task is explicit that classification is one of the three
signals to combine ("combine OCR/extraction/classification confidences into
field and analysis tiers"), not an optional extra - found live, 2026-09-10,
as a real gap: `_classifying` already computed and persisted
`Analysis.category_confidence`/`jurisdiction_confidence`, but nothing ever
read them back into the final tier before this.

    High   | all rule-relevant fields >= 0.90 and verified | auto-complete
    Medium | any field 0.70-0.90                           | "verify" queue
    Low    | any field < 0.70, or extraction/OCR failure,
             or conflicting duplicates                      | mandatory review

Field confidence is `min(ocr_conf, extraction_conf)` "adjusted by the
verification gate" (§14): a field that P3-T6 demoted (`verified is False`)
or that was never found on the label at all (`value_raw is None`) cannot
average its way to a passing score through an unrelated high OCR/extraction
confidence - it is forced straight to Low, with a stated reason, matching
the acceptance criterion's own test line: "a missing rule-relevant field
forces Low."

**Scoping decision, documented rather than silently narrowed:** §14 defines
the analysis tier as "the worst tier among fields any triggered rule depends
on." No ruleset can be resolved yet (`rule_eval` is still a placeholder,
blocked on D-01 - see `app.analysis.stages`), so there is no set of
"triggered rules" to consult. Until that exists, this module computes the
tier over *every* field this extraction persisted, which is the safe
superset of whatever subset a real ruleset will eventually narrow it to -
never a smaller set than the real one, so this can only make the tier equal
or more conservative, never falsely High. `compute_analysis_tier` is the one
place that narrowing will happen once `rule_eval` is real.

CLASSIFICATION AS A SIGNAL
--------------------------
`app.classification.classifier.classify()` (P3-T9) never guesses - it either
resolves a category/jurisdiction with a real, computed confidence, or it
abstains entirely (`Analysis.category is None`). Both outcomes must affect
the final tier, not just the per-field ones: an abstained classification
means nothing downstream can be trusted to route to the right jurisdiction's
rules at all (`rule_eval` needs a resolved category to even pick a
ruleset), so it forces Low exactly like a missing/demoted field - the same
"absence of data is never treated as compliance" principle (§1), applied to
a signal that happens to live on `Analysis` rather than `ExtractedField`.

A *resolved* classification is graded on its own calibrated scale, not
force-fit onto the 0.90/0.70 field bands above: `classify()` only ever
returns non-abstained once each signal already clears its own threshold
(`CATEGORY_CONFIDENCE_THRESHOLD`/`JURISDICTION_CONFIDENCE_THRESHOLD`, both
well under 0.70), so reusing the field bands verbatim would force every
successful classification to Low regardless of how confident it actually
was - not a real signal, just noise from comparing two differently-scaled
numbers. Instead: confidence `>= 1.0` (every available signal agreed - an
exact, fully-recognized user-declared hint, or every extractable fact
present) is High-eligible; anything short of that ceiling but still past
the classifier's own abstention threshold is a real, if incomplete, basis
for a decision and caps the tier at Medium rather than authorizing full
auto-completion on a partially-inferred jurisdiction.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.models import Analysis, ConfidenceTier
from app.extraction.models import ExtractedField, Extraction
from app.vision.models import OcrTokenRow

CONFIDENCE_VERSION = "1.0.0"

HIGH_THRESHOLD = 0.90
MEDIUM_THRESHOLD = 0.70

_TIER_RANK: dict[ConfidenceTier, int] = {
    ConfidenceTier.LOW: 0,
    ConfidenceTier.MEDIUM: 1,
    ConfidenceTier.HIGH: 2,
}


def tier_for_confidence(confidence: float) -> ConfidenceTier:
    """The threshold table alone, with no field-specific overrides - those
    live in `compute_analysis_tier`, which is the only place a field can be
    forced to Low regardless of its numeric score."""
    if confidence >= HIGH_THRESHOLD:
        return ConfidenceTier.HIGH
    if confidence >= MEDIUM_THRESHOLD:
        return ConfidenceTier.MEDIUM
    return ConfidenceTier.LOW


def field_confidence(ocr_conf: float, extraction_conf: float) -> float:
    """§14: 'the *field* confidence is min(ocr_conf, extraction_conf)'."""
    return min(ocr_conf, extraction_conf)


@dataclass(frozen=True)
class FieldTier:
    field_path: str
    confidence: float
    tier: ConfidenceTier
    reason: str | None = None


@dataclass(frozen=True)
class AnalysisTierResult:
    tier: ConfidenceTier
    fields: tuple[FieldTier, ...]


def _cited_token_confidence(db: Session, token_ids: list[str]) -> float:
    """The OCR half of a field's confidence: the worst (not average) of every
    cited token's own confidence, matching the codebase's standing
    conservative-by-default choice (the same reasoning P3-T6's 0.85 match
    threshold and P3-T9's abstention-over-guessing both follow) - one badly
    read token should not be diluted away by several well-read ones."""
    if not token_ids:
        return 0.0
    ids = [uuid.UUID(t) for t in token_ids]
    confidences = db.scalars(
        select(OcrTokenRow.confidence).where(OcrTokenRow.id.in_(ids))
    ).all()
    if not confidences:
        return 0.0
    return min(confidences)


_ABSTENTION_REASON = (
    "classification abstained - no category/jurisdiction could be determined "
    "with enough confidence to route to rules safely"
)


def _classification_tiers(analysis: Analysis) -> list[FieldTier]:
    """The classification (P3-T9) half of the final tier - see the module
    docstring's "CLASSIFICATION AS A SIGNAL" section for why this is graded
    on its own scale rather than the OCR/extraction bands. Two entries
    (category, jurisdiction), not one combined score, so a caller can see
    *which* signal was weak - the same explainability the per-field entries
    already give."""
    if analysis.category is None:
        return [
            FieldTier(
                field_path="classification.category",
                confidence=analysis.category_confidence,
                tier=ConfidenceTier.LOW,
                reason=_ABSTENTION_REASON,
            ),
            FieldTier(
                field_path="classification.jurisdiction",
                confidence=analysis.jurisdiction_confidence,
                tier=ConfidenceTier.LOW,
                reason=_ABSTENTION_REASON,
            ),
        ]
    return [
        FieldTier(
            field_path="classification.category",
            confidence=analysis.category_confidence,
            tier=(
                ConfidenceTier.HIGH
                if analysis.category_confidence >= 1.0
                else ConfidenceTier.MEDIUM
            ),
        ),
        FieldTier(
            field_path="classification.jurisdiction",
            confidence=analysis.jurisdiction_confidence,
            tier=(
                ConfidenceTier.HIGH
                if analysis.jurisdiction_confidence >= 1.0
                else ConfidenceTier.MEDIUM
            ),
        ),
    ]


def compute_analysis_tier(
    db: Session, *, extraction: Extraction, analysis: Analysis
) -> AnalysisTierResult:
    """Deterministic and explainable, per the acceptance criterion: every
    field's own confidence, tier, and (when forced) the reason it was forced
    are all returned, not just the final rolled-up tier. `analysis` supplies
    the classification (P3-T9) half of the signal - `category_confidence`/
    `jurisdiction_confidence` live on `Analysis`, not `ExtractedField`."""
    rows = db.scalars(
        select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
    ).all()

    if not rows:
        missing = FieldTier(
            field_path="*",
            confidence=0.0,
            tier=ConfidenceTier.LOW,
            reason="no fields were extracted",
        )
        no_fields = [missing, *_classification_tiers(analysis)]
        return AnalysisTierResult(tier=ConfidenceTier.LOW, fields=tuple(no_fields))

    fields: list[FieldTier] = []
    for row in rows:
        if row.value_raw is None:
            fields.append(
                FieldTier(
                    field_path=row.field_path,
                    confidence=0.0,
                    tier=ConfidenceTier.LOW,
                    reason="field was not found on the label",
                )
            )
            continue
        if row.verified is False:
            fields.append(
                FieldTier(
                    field_path=row.field_path,
                    confidence=0.0,
                    tier=ConfidenceTier.LOW,
                    reason="field failed evidence verification",
                )
            )
            continue

        ocr_conf = _cited_token_confidence(db, row.cited_token_ids)
        confidence = field_confidence(ocr_conf, row.confidence)
        fields.append(
            FieldTier(
                field_path=row.field_path,
                confidence=confidence,
                tier=tier_for_confidence(confidence),
            )
        )

    fields.extend(_classification_tiers(analysis))
    overall = min(fields, key=lambda f: _TIER_RANK[f.tier]).tier
    return AnalysisTierResult(tier=overall, fields=tuple(fields))
