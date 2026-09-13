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

"conflicting duplicates" became real in P7-T4 (it was documentation-only
here for four phases): `app.extraction.conflicts` detects a field whose own
cited OCR tokens disagree under that field's real normalizer, and this
module forces it to Low. See that module for exactly which conflict shape
is detected and which is deliberately not.

Field confidence is `min(ocr_conf, extraction_conf)` "adjusted by the
verification gate" (§14): a field that P3-T6 demoted (`verified is False`)
or that was never found on the label at all (`value_raw is None`) cannot
average its way to a passing score through an unrelated high OCR/extraction
confidence - it is forced straight to Low, with a stated reason, matching
the acceptance criterion's own test line: "a missing rule-relevant field
forces Low."

**Narrowing, now real (2026-09-10):** §14 defines the analysis tier as "the
worst tier among fields any triggered rule depends on." `rule_eval` is real
now (P5-T4) and a real ruleset exists (P4-T5/D-01), so `compute_analysis_tier`
narrows to exactly that set whenever it can: the union of every applicable
`Finding`'s own `evidence_fields` (from `Finding.details`, the evaluator's
own record of which fields that rule depended on) for this analysis -
`not_applicable` findings are excluded, since their `evidence_fields` name
fields a rule *would* have checked for a different jurisdiction/category,
never ones this label was actually judged against. When no findings exist
at all - classification abstained, or no ruleset was ever published for this
jurisdiction/category (still true for every jurisdiction but IN/
packaged_food today) - there is no real "triggered rules" set to narrow to,
so this falls back to the original, documented-safe superset: every field
this extraction persisted. That superset can only make the tier equal or
more conservative than the narrowed set would, never falsely High, which is
exactly why it was the right default before real rule content existed and
remains the right fallback now for every jurisdiction D-01 hasn't resolved.

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
from app.extraction import conflicts
from app.extraction.models import ExtractedField, Extraction
from app.findings.models import Finding
from app.rules.evaluator import FindingStatus
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


def _cited_token_texts(db: Session, token_ids: list[str]) -> list[str]:
    """Every cited token's own text, for `app.extraction.conflicts` to
    canonicalize - see that module for why a field's citations disagreeing
    with each other is the one conflict shape this system can detect
    without false positives."""
    if not token_ids:
        return []
    ids = [uuid.UUID(t) for t in token_ids]
    return list(db.scalars(select(OcrTokenRow.text).where(OcrTokenRow.id.in_(ids))).all())


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


def _rule_relevant_fields(db: Session, analysis: Analysis) -> set[str] | None:
    """The union of every applicable `Finding`'s own `evidence_fields` for
    this analysis, or `None` when there is nothing to narrow to (no findings
    exist at all - classification abstained, or `rule_eval` found no
    published ruleset for this jurisdiction/category). `not_applicable`
    findings are excluded: their `evidence_fields` name fields a rule *would*
    check under a different jurisdiction/category, never ones this label was
    actually judged against."""
    findings = db.scalars(
        select(Finding).where(
            Finding.analysis_id == analysis.id,
            Finding.status != FindingStatus.NOT_APPLICABLE,
        )
    ).all()
    if not findings:
        return None
    relevant: set[str] = set()
    for finding in findings:
        evidence_fields = finding.details.get("evidence_fields")
        if isinstance(evidence_fields, list):
            relevant.update(path for path in evidence_fields if isinstance(path, str))
    return relevant


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
    `jurisdiction_confidence` live on `Analysis`, not `ExtractedField`. Also
    supplies the narrowing (P5-T4/P4-T5) half: see `_rule_relevant_fields`
    and this module's own docstring for why a field no triggered rule
    depends on no longer drags the tier down once real findings exist to
    narrow to."""
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

    # An empty (but non-`None`) set - every finding that exists is
    # `not_applicable` - is treated the same as `None`: narrowing to nothing
    # would silently drop every field from the tier calculation, which is
    # never safe, so fall back to the full superset in that case too.
    relevant_fields = _rule_relevant_fields(db, analysis) or None
    if relevant_fields is not None:
        rows = [row for row in rows if row.field_path in relevant_fields]

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

        # "or conflicting duplicates -> mandatory review" (§14's routing
        # table, this module's own docstring) - real as of P7-T4, not just
        # documented. Checked before the numeric bands, exactly like the
        # not-found/failed-verification forcings above: a field whose own
        # citations disagree is untrustworthy no matter how confidently
        # either token was read.
        conflict = conflicts.find_conflicting_citation(
            row.field_path, _cited_token_texts(db, row.cited_token_ids)
        )
        if conflict is not None:
            fields.append(
                FieldTier(
                    field_path=row.field_path,
                    confidence=0.0,
                    tier=ConfidenceTier.LOW,
                    reason=f"conflicting duplicate values on the label ({conflict})",
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
