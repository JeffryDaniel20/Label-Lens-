"""Category and jurisdiction classification (P3-T9).

Classifies a product version's category and applicable jurisdiction(s) from
its extracted `LabelFacts` plus optional user-declared hints
(`Product.category_hint`, `Product.market_codes`). Classification either
succeeds with a category, one or more jurisdictions, and a confidence, or it
explicitly abstains with a reason - there is no third option. This is a hard
architectural requirement, not a style choice: IMPLEMENTATION.md's own
acceptance criterion for this task is "no analysis proceeds to rules with a
guessed category" - a low-confidence guess routed into the rule engine would
silently apply the wrong jurisdiction's law to a label, which is worse than
asking a human to confirm it.

Only one category is recognized in this MVP - `packaged_food` - matching
IMPLEMENTATION.md's explicit "one jurisdiction + one category" MVP scope
(section 1). Two jurisdiction codes are recognized, symmetrically and
without preference: `IN` and `EU`, the two jurisdictions this project's own
rule-namespace convention (`in-fssai-food`, `eu-1169-food`, section 10)
already anticipates. Which one gets a real rule pack built first is D-01, a
decision this module does not make or presume - it only recognizes both
when asked to classify, matching IMPLEMENTATION.md's note that "the design
is jurisdiction-agnostic."

Not yet wired into any HTTP endpoint or the ingestion pipeline - like P3-T1
and P3-T4, this is a pure, tested library. It will be called from Phase 5's
analysis orchestration once that exists, with `UserHints` built from a real
`Product` row.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.extraction.facts import LabelFacts

CLASSIFIER_VERSION: str = "1.0.0"

CANONICAL_CATEGORIES: frozenset[str] = frozenset({"packaged_food"})
SUPPORTED_JURISDICTIONS: frozenset[str] = frozenset({"IN", "EU"})

# A category needs at least 2 of these 3 core fact groups actually extracted
# (not merely attempted) before it is treated as recognizably "a packaged
# food label" at all, rather than an unrelated image.
CATEGORY_CONFIDENCE_THRESHOLD: float = 0.66
JURISDICTION_CONFIDENCE_THRESHOLD: float = 0.5

_JURISDICTION_ADDRESS_KEYWORDS: dict[str, tuple[str, ...]] = {
    "IN": (
        "india",
        "bharat",
        "mumbai",
        "delhi",
        "bengaluru",
        "bangalore",
        "chennai",
        "kolkata",
        "hyderabad",
        "pune",
    ),
    "EU": (
        "germany",
        "france",
        "spain",
        "italy",
        "netherlands",
        "belgium",
        "poland",
        "portugal",
        "ireland",
        "austria",
        "european union",
    ),
}


@dataclass(slots=True, frozen=True)
class UserHints:
    """User-declared hints, sourced from a `Product` row's `category_hint`
    and `market_codes` - the strongest signal this classifier has access to,
    since it is a direct statement of intent rather than an inference."""

    category_hint: str | None = None
    market_codes: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class ClassificationResult:
    category: str | None
    jurisdictions: tuple[str, ...]
    category_confidence: float
    jurisdiction_confidence: float
    abstained: bool
    reason: str | None = None


def _category_confidence(facts: LabelFacts, hints: UserHints) -> float:
    signals = (
        facts.ingredients.items.value is not None,
        facts.nutrition.rows.value is not None,
        facts.quantity.net_quantity.value is not None,
    )
    fact_confidence = sum(signals) / len(signals)
    if hints.category_hint in CANONICAL_CATEGORIES:
        # An explicit, recognized user statement of intent can carry a
        # marginal case over the threshold, but never substitutes entirely
        # for extracted evidence - it is combined with, not instead of, facts.
        return max(fact_confidence, 0.9)
    return fact_confidence


def _address_text(facts: LabelFacts) -> str:
    addresses = facts.addresses.items.value or ()
    return " ".join(address.text for address in addresses).lower()


def _jurisdiction_from_hints(hints: UserHints) -> tuple[tuple[str, ...], float]:
    if not hints.market_codes:
        return (), 0.0
    declared = tuple(dict.fromkeys(code.upper() for code in hints.market_codes))
    recognized = tuple(code for code in declared if code in SUPPORTED_JURISDICTIONS)
    if not recognized:
        return (), 0.0
    # Every declared code was recognized -> full trust; a partial match
    # (some codes unrecognized) still counts the recognized ones, but with
    # reduced confidence, since the input was not entirely as expected.
    return recognized, 1.0 if recognized == declared else 0.6


def _jurisdiction_from_address(facts: LabelFacts) -> tuple[tuple[str, ...], float]:
    text = _address_text(facts)
    if not text:
        return (), 0.0
    matched = tuple(
        code
        for code, keywords in _JURISDICTION_ADDRESS_KEYWORDS.items()
        if any(keyword in text for keyword in keywords)
    )
    return (matched, 0.5) if matched else ((), 0.0)


def classify(facts: LabelFacts, hints: UserHints | None = None) -> ClassificationResult:
    """Classify `facts` (and optional `hints`) into a category and
    jurisdiction(s), or abstain with a stated reason. Never guesses."""
    hints = hints if hints is not None else UserHints()

    if hints.category_hint is not None and hints.category_hint not in CANONICAL_CATEGORIES:
        # An explicit but unrecognized hint is itself a reason to abstain,
        # rather than silently falling back to a fact-only inference the
        # user's own statement may directly contradict.
        return ClassificationResult(
            category=None,
            jurisdictions=(),
            category_confidence=0.0,
            jurisdiction_confidence=0.0,
            abstained=True,
            reason=f"Unrecognized category hint: {hints.category_hint!r}.",
        )

    category_confidence = _category_confidence(facts, hints)
    category = "packaged_food" if category_confidence >= CATEGORY_CONFIDENCE_THRESHOLD else None

    jurisdictions, jurisdiction_confidence = _jurisdiction_from_hints(hints)
    if not jurisdictions:
        jurisdictions, jurisdiction_confidence = _jurisdiction_from_address(facts)

    if category is None or jurisdiction_confidence < JURISDICTION_CONFIDENCE_THRESHOLD:
        reasons: list[str] = []
        if category is None:
            reasons.append(
                f"Category confidence {category_confidence:.2f} is below the "
                f"{CATEGORY_CONFIDENCE_THRESHOLD:.2f} threshold - not enough core "
                "label facts (ingredients, nutrition, net quantity) were extracted."
            )
        if jurisdiction_confidence < JURISDICTION_CONFIDENCE_THRESHOLD:
            reasons.append(
                f"Jurisdiction confidence {jurisdiction_confidence:.2f} is below the "
                f"{JURISDICTION_CONFIDENCE_THRESHOLD:.2f} threshold - no declared market "
                "code and no recognizable jurisdiction signal in the extracted address."
            )
        return ClassificationResult(
            category=None,
            jurisdictions=(),
            category_confidence=category_confidence,
            jurisdiction_confidence=jurisdiction_confidence,
            abstained=True,
            reason=" ".join(reasons),
        )

    return ClassificationResult(
        category=category,
        jurisdictions=jurisdictions,
        category_confidence=category_confidence,
        jurisdiction_confidence=jurisdiction_confidence,
        abstained=False,
    )
