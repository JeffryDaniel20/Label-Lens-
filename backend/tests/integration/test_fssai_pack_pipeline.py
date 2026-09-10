"""End-to-end proof that a real analysis, run through the real pipeline,
produces real findings against the real `in-fssai-food` v1.0.0 pack (P4-T5) -
not a synthetic test rule (see `tests/integration/test_rule_eval_stage.py`
for that, and `tests/rules/test_in_fssai_food_pack.py` for the pack's own
content tests in isolation from the pipeline).

D-01 (first jurisdiction) is resolved to India/FSSAI by explicit user
instruction (2026-09-10); this file is the "does it actually reach a real
regulatory finding, end to end" proof that closes the loop opened by P5-T4's
own `_rule_eval` wiring.
"""

# ruff: noqa: F811 - each `basic` test parameter below is pytest fixture
# injection by name, not a redefinition of the `basic` fixture imported at
# module scope for pytest to discover it in this file.

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select

from app.analysis.models import ConfidenceTier
from app.analysis.stages import (
    _classifying,
    _evidence_verification,
    _normalizing,
    _rule_eval,
    _scoring,
)
from app.confidence.tiers import compute_analysis_tier
from app.findings.models import Finding, FindingEvidence
from app.rules.loader import load_pack_from_directory
from app.rules.publish import publish_pack
from app.vision.models import OcrResult, OcrTokenRow
from tests.integration.test_pipeline_stages import (
    _extraction_for,
    basic,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.integration

PACK_DIR = Path(__file__).resolve().parents[2] / "app/rulesets/in-fssai-food/v1.0.0"

# Every value below is an exact copy of its backing token's text (not a
# paraphrase), so P3-T6's evidence-verification gate resolves every citation
# instead of demoting it - this test is about `rule_eval`, not re-testing
# fuzzy-match tolerance.
_TOKENS = (
    "Sugar, Wheat Flour, Cocoa Solids",  # 0: ingredients
    "250 g",  # 1: net quantity
    "01/2026",  # 2: manufacture date
    "12/2027",  # 3: expiry date
    "Contains: Wheat",  # 4: allergens (genuine)
    "30 g",  # 5: nutrition serving size
    "B12345",  # 6: batch number
    "Contains: Unobtainium",  # 7: allergens (a genuinely garbled but real citation)
    "Energy 450.0kcal",  # 8: nutrition row (must match joined_row's exact formatting)
    "manufacturer: ABC Foods Pvt Ltd, Pune, India",  # 9: address (must match joined_row's format)
    "English",  # 10: language marker (a genuine substring match for "en")
)

COMPLIANT_LABEL_JSON = """
{
  "ingredients_declared_text": {"value": "Sugar, Wheat Flour, Cocoa Solids",
                                "not_found_reason": null, "token_ids": [0], "confidence": 0.95},
  "allergens_declaration_text": {"value": "Contains: Wheat", "not_found_reason": null,
                                 "token_ids": [4], "confidence": 0.9},
  "allergens_declared": {"values": ["Wheat"], "not_found_reason": null,
                         "token_ids": [4], "confidence": 0.9},
  "nutrition_serving_size": {"value": "30 g", "not_found_reason": null,
                             "token_ids": [5], "confidence": 0.9},
  "nutrition_rows": [
    {"nutrient": "Energy", "unit": "kcal", "per_100g": 450.0, "per_serving": 135.0,
     "token_ids": [8]}
  ],
  "nutrition_rows_not_found_reason": null,
  "quantity_net_quantity": {"value": "250 g", "not_found_reason": null,
                            "token_ids": [1], "confidence": 0.99},
  "dates_manufacture": {"value": "01/2026", "not_found_reason": null,
                        "token_ids": [2], "confidence": 0.9},
  "dates_expiry_or_best_before": {"value": "12/2027", "not_found_reason": null,
                                  "token_ids": [3], "confidence": 0.95},
  "dates_batch_number": {"value": "B12345", "not_found_reason": null,
                         "token_ids": [6], "confidence": 0.9},
  "claims": [], "claims_not_found_reason": "no claims printed",
  "addresses": [{"role": "manufacturer", "text": "ABC Foods Pvt Ltd, Pune, India",
                 "token_ids": [9]}],
  "addresses_not_found_reason": null,
  "languages_detected": {"values": ["en"], "not_found_reason": null,
                         "token_ids": [10], "confidence": 0.9}
}
"""


def _add_full_ocr_tokens(db, org, page) -> None:
    result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="stub",
        engine_version="1",
        avg_confidence=0.9,
        raw=[],
    )
    db.add(result)
    db.flush()
    for i, text in enumerate(_TOKENS):
        db.add(
            OcrTokenRow(
                organization_id=org.id,
                file_page_id=page.id,
                ocr_result_id=result.id,
                text=text,
                confidence=0.9,
                x1=float(i),
                y1=0.0,
                x2=float(i + 1),
                y2=1.0,
                line_no=i,
            )
        )
    db.flush()


def _run_to_rule_eval(db, org, analysis, *, text: str) -> None:
    _extraction_for(db, org, analysis, text=text)
    assert _evidence_verification(db, analysis) is None
    db.commit()
    assert _normalizing(db, analysis) is None
    db.commit()
    assert _classifying(db, analysis) is None
    db.commit()


class TestRealAnalysisAgainstTheRealFssaiPack:
    def test_a_compliant_label_produces_real_passing_findings_with_real_citations(
        self, db, basic
    ) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)

        pack = load_pack_from_directory(PACK_DIR)
        ruleset = publish_pack(db, pack)
        db.commit()

        _run_to_rule_eval(db, org, analysis, text=COMPLIANT_LABEL_JSON)
        db.refresh(analysis)
        assert analysis.category == "packaged_food"
        assert "IN" in analysis.jurisdictions

        result = _rule_eval(db, analysis)
        db.commit()
        assert result is None

        db.refresh(analysis)
        assert analysis.ruleset_version_id == ruleset.id

        findings = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        assert len(findings) == 11
        by_key = {f.rule_key: f for f in findings}
        assert set(by_key) == {r.rule_key for r in pack.rules}
        for finding in findings:
            assert finding.status == "pass", (finding.rule_key, finding.status)
            assert finding.ruleset_id == ruleset.id
            # A real citation traceable to a real regulation clause, not a
            # placeholder - the literal "a reviewer can trace each rule to
            # its clause" acceptance criterion, proven through a finding row
            # a real analysis actually produced.
            rule = next(r for r in pack.rules if r.rule_key == finding.rule_key)
            assert "Regulation" in rule.citation

        # Every pass/fail finding resolves to a real evidence chain (P5-T4's
        # own structural invariant) - spot-check one.
        net_qty_finding = by_key["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"]
        edges = db.scalars(
            select(FindingEvidence).where(FindingEvidence.finding_id == net_qty_finding.id)
        ).all()
        assert len(edges) >= 1

    def test_a_field_no_fssai_rule_depends_on_no_longer_blocks_auto_completion(
        self, db, basic
    ) -> None:
        """2026-09-10: `compute_analysis_tier`'s own long-documented "next
        step" - narrow to exactly the fields real findings depend on, once
        `rule_eval` is real - proven through the real pipeline, not just the
        isolated unit-level fixtures in `test_confidence_tiers.py`.
        `COMPLIANT_LABEL_JSON`'s own `claims` is genuinely empty (a
        completely normal, non-problematic label that simply prints no
        promotional claims), which resolves to a `claims.items` field with
        no value at all - before this narrowing existed, that alone would
        have forced the whole analysis to Low/mandatory-review even though
        no FSSAI rule in this pack has ever checked `claims.items` and every
        field an FSSAI rule *does* depend on is cleanly, confidently
        extracted."""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)

        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        extraction = _extraction_for(db, org, analysis, text=COMPLIANT_LABEL_JSON)
        assert _evidence_verification(db, analysis) is None
        db.commit()
        assert _normalizing(db, analysis) is None
        db.commit()
        assert _classifying(db, analysis) is None
        db.commit()
        assert _rule_eval(db, analysis) is None
        db.commit()

        db.refresh(extraction)
        # Confirm the premise directly: `claims.items` really did resolve to
        # "not found" - the exact state that used to force Low regardless of
        # narrowing - and is now excluded from the tier calculation entirely
        # since no rule in this pack depends on it.
        tier_result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)
        assert not any(f.field_path == "claims.items" for f in tier_result.fields)

        db.refresh(analysis)
        next_state = _scoring(db, analysis)
        db.commit()
        db.refresh(analysis)

        assert analysis.confidence_tier is ConfidenceTier.HIGH
        assert next_state is None  # High tier falls through to the default successor

    def test_a_label_with_missing_and_garbled_declarations_tells_fail_from_insufficient_data(
        self, db, basic
    ) -> None:
        """A label missing its batch number entirely (a real, honest
        `not_found_reason` from the extractor - `insufficient_data`) versus
        one that genuinely prints a garbled, unrecognized allergen name (a
        real, verifiable citation to real - if wrong - printed text -
        `fail`): the pack must tell these two apart, not collapse them into
        one generic non-pass status. Every citation here still fuzzy-matches
        its backing token exactly as `TestRealAnalysisAgainstTheRealFssaiPack`
        does - the "garbled" content is genuinely what the (fictional) label
        prints, not a mismatched citation P3-T6 would demote."""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)

        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        broken = COMPLIANT_LABEL_JSON.replace(
            '"allergens_declared": {"values": ["Wheat"], "not_found_reason": null,\n'
            '                         "token_ids": [4], "confidence": 0.9}',
            '"allergens_declared": {"values": ["Unobtainium"], "not_found_reason": null,\n'
            '                         "token_ids": [7], "confidence": 0.9}',
        ).replace(
            '"allergens_declaration_text": {"value": "Contains: Wheat", '
            '"not_found_reason": null,\n'
            '                                 "token_ids": [4], "confidence": 0.9}',
            '"allergens_declaration_text": {"value": "Contains: Unobtainium", '
            '"not_found_reason": null,\n'
            '                                 "token_ids": [7], "confidence": 0.9}',
        ).replace(
            '"dates_batch_number": {"value": "B12345", "not_found_reason": null,\n'
            '                         "token_ids": [6], "confidence": 0.9}',
            '"dates_batch_number": {"value": null, "not_found_reason": "not printed",\n'
            '                         "token_ids": [], "confidence": 0.0}',
        )
        assert broken != COMPLIANT_LABEL_JSON

        _run_to_rule_eval(db, org, analysis, text=broken)
        _rule_eval(db, analysis)
        db.commit()

        findings = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        by_key = {f.rule_key: f for f in findings}
        assert by_key["IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED"].status == "fail"
        assert by_key["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"].status == "insufficient_data"
        assert by_key["IN-FSSAI-FOOD-INGREDIENTS-LIST-DECLARED"].status == "pass"
        assert by_key["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"].status == "pass"
