"""Shared rig for the adversarial suite: build an adversarial label and run
it through the REAL pipeline.

Deliberately reuses `evals.runner` (P7-T3) rather than growing a second
pipeline driver: that module already drives the exact real stage functions
(`_evidence_verification -> _normalizing -> _classifying -> _rule_eval ->
_scoring`) production uses, and returns precisely what an adversarial
assertion needs - the real `ExtractedField` values and `verified` flags,
the real `Finding` statuses, and the real confidence tier. A second driver
here would be one more thing to keep in sync with the pipeline, and the
whole point of this suite is that it exercises the real one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.rules.loader import load_pack_from_directory
from app.rules.publish import publish_pack
from evals.dataset import GoldenCase, OcrTokenFixture
from evals.runner import CaseResult, run_case

_PACK_DIR = (
    Path(__file__).resolve().parents[3] / "app" / "rulesets" / "in-fssai-food" / "v1.0.0"
)

# The clean, fully-compliant baseline every adversarial case mutates from -
# the same real label content `tests/integration/test_fssai_pack_pipeline.py`
# and the P7-T3 golden dataset both use, so "what changed" in an adversarial
# case is exactly the adversarial edit and nothing else.
BASELINE_TOKENS: tuple[str, ...] = (
    "Sugar, Wheat Flour, Cocoa Solids",
    "250 g",
    "01/2026",
    "12/2027",
    "Contains: Wheat",
    "30 g",
    "B12345",
    "Contains: Unobtainium",
    "Energy 450.0kcal",
    "manufacturer: ABC Foods Pvt Ltd, Pune, India",
    "English",
)


def baseline_extraction() -> dict[str, Any]:
    """A fresh, mutable copy of the compliant extraction envelope."""
    return {
        "ingredients_declared_text": {
            "value": "Sugar, Wheat Flour, Cocoa Solids",
            "not_found_reason": None,
            "token_ids": [0],
            "confidence": 0.95,
        },
        "allergens_declaration_text": {
            "value": "Contains: Wheat",
            "not_found_reason": None,
            "token_ids": [4],
            "confidence": 0.9,
        },
        "allergens_declared": {
            "values": ["Wheat"],
            "not_found_reason": None,
            "token_ids": [4],
            "confidence": 0.9,
        },
        "nutrition_serving_size": {
            "value": "30 g",
            "not_found_reason": None,
            "token_ids": [5],
            "confidence": 0.9,
        },
        "nutrition_rows": [
            {
                "nutrient": "Energy",
                "unit": "kcal",
                "per_100g": 450.0,
                "per_serving": 135.0,
                "token_ids": [8],
            }
        ],
        "nutrition_rows_not_found_reason": None,
        "quantity_net_quantity": {
            "value": "250 g",
            "not_found_reason": None,
            "token_ids": [1],
            "confidence": 0.99,
        },
        "dates_manufacture": {
            "value": "01/2026",
            "not_found_reason": None,
            "token_ids": [2],
            "confidence": 0.9,
        },
        "dates_expiry_or_best_before": {
            "value": "12/2027",
            "not_found_reason": None,
            "token_ids": [3],
            "confidence": 0.95,
        },
        "dates_batch_number": {
            "value": "B12345",
            "not_found_reason": None,
            "token_ids": [6],
            "confidence": 0.9,
        },
        "claims": [],
        "claims_not_found_reason": "no claims printed",
        "addresses": [
            {
                "role": "manufacturer",
                "text": "ABC Foods Pvt Ltd, Pune, India",
                "token_ids": [9],
            }
        ],
        "addresses_not_found_reason": None,
        "languages_detected": {
            "values": ["en"],
            "not_found_reason": None,
            "token_ids": [10],
            "confidence": 0.9,
        },
    }


def not_found(reason: str = "not printed") -> dict[str, Any]:
    """An honest "this field is not on the label" envelope entry."""
    return {"value": None, "not_found_reason": reason, "token_ids": [], "confidence": 0.0}


def adversarial_case(
    *,
    case_id: str,
    tokens: tuple[str, ...] = BASELINE_TOKENS,
    extraction: dict[str, Any] | None = None,
    token_confidence: float = 0.9,
    category_hint: str = "packaged_food",
    market_codes: tuple[str, ...] = ("IN",),
) -> GoldenCase:
    """One adversarial label, in the same `GoldenCase` shape the P7-T3 eval
    runner already knows how to drive. `expected_*` are left empty: an
    adversarial case asserts a *behaviour* (routed to review, never `pass`,
    verdict unchanged), not a full ground-truth match."""
    return GoldenCase(
        case_id=case_id,
        description=f"adversarial: {case_id}",
        image_quality="adversarial",
        category_hint=category_hint,
        market_codes=market_codes,
        extraction_json=json.dumps(extraction if extraction is not None else baseline_extraction()),
        expected_fields={},
        expected_findings={},
        ocr_tokens=tuple(
            OcrTokenFixture(
                text=text,
                x1=float(i),
                y1=0.0,
                x2=float(i + 1),
                y2=1.0,
                line_no=i,
                confidence=token_confidence,
            )
            for i, text in enumerate(tokens)
        ),
    )


def run_adversarial(db: Session, case: GoldenCase) -> CaseResult:
    """Publishes the real `in-fssai-food` pack (idempotent - `publish_pack`
    returns the existing row for identical content) and runs the case.

    Publishing is not optional and not a detail: without a published
    ruleset `_rule_eval` produces no findings at all, which makes
    `compute_analysis_tier` fall back to scoring *every* extracted field
    instead of narrowing to rule-relevant ones - and the baseline label's
    honestly-absent `claims.items` then forces every analysis to Low. Every
    "routes to review" assertion in this suite would pass for that reason
    rather than the adversarial one it claims to test. Caught exactly that
    way while building this suite, hence this comment.
    """
    publish_pack(db, load_pack_from_directory(_PACK_DIR))
    db.commit()
    return run_case(db, case)
