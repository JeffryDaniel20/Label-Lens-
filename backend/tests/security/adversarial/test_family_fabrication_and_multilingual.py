"""Section 23's "Fabrication bait" and "Multilingual" families.

Fabrication bait - *no invented values; `not_found` with reason; category
classification refuses.*
Multilingual - *per-language extraction, language-coverage rules evaluated.*
"""

from __future__ import annotations

import pytest

from tests.security.adversarial.rig import (
    BASELINE_TOKENS,
    adversarial_case,
    baseline_extraction,
    not_found,
    run_adversarial,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]


class TestFabricationBait:
    def test_an_absent_field_is_not_found_with_a_reason_never_invented(self, db) -> None:
        """The bait: every *other* field on the packet is present and
        plausible, so a model inclined to pattern-complete would happily
        invent the missing one."""
        extraction = baseline_extraction()
        extraction["dates_batch_number"] = not_found("no batch code printed anywhere")

        result = run_adversarial(
            db, adversarial_case(case_id="bait-absent-field", extraction=extraction)
        )

        assert result.actual_fields["dates.batch_number"] is None
        assert result.actual_findings["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"] == (
            "insufficient_data"
        )

    def test_an_entirely_blank_label_invents_nothing(self, db) -> None:
        """A blank page: every field honestly absent. Nothing may be
        fabricated, and nothing may reach a `pass`."""
        blank = {
            key: (not_found("blank page - nothing printed") if isinstance(value, dict) else value)
            for key, value in baseline_extraction().items()
        }
        blank["allergens_declared"] = {
            "values": [],
            "not_found_reason": "blank page",
            "token_ids": [],
            "confidence": 0.0,
        }
        blank["languages_detected"] = {
            "values": [],
            "not_found_reason": "blank page",
            "token_ids": [],
            "confidence": 0.0,
        }
        blank["nutrition_rows"] = []
        blank["nutrition_rows_not_found_reason"] = "blank page"
        blank["claims"] = []
        blank["addresses"] = []
        blank["addresses_not_found_reason"] = "blank page"

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="bait-blank-page", tokens=("",), extraction=blank
            ),
        )

        assert all(value is None for value in result.actual_fields.values())
        assert "pass" not in set(result.actual_findings.values()), (
            "a blank page must never produce a passing compliance verdict"
        )
        assert result.confidence_tier == "low"

    def test_an_unclassifiable_label_never_reaches_a_passing_verdict(self, db) -> None:
        """End to end: with no category hint and nothing food-shaped
        extracted, classification abstains, `rule_eval` has no ruleset to
        resolve, and the analysis routes to review with zero findings -
        never a silent completion."""
        blank = {
            key: (not_found("unrelated image") if isinstance(value, dict) else value)
            for key, value in baseline_extraction().items()
        }
        blank["allergens_declared"] = {
            "values": [],
            "not_found_reason": "unrelated image",
            "token_ids": [],
            "confidence": 0.0,
        }
        blank["languages_detected"] = {
            "values": [],
            "not_found_reason": "unrelated image",
            "token_ids": [],
            "confidence": 0.0,
        }
        blank["nutrition_rows"] = []
        blank["nutrition_rows_not_found_reason"] = "unrelated image"
        blank["claims"] = []
        blank["addresses"] = []
        blank["addresses_not_found_reason"] = "unrelated image"

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="bait-unrelated-image",
                tokens=("a photograph of a cat",),
                extraction=blank,
                category_hint="",
                market_codes=(),
            ),
        )

        assert result.actual_findings == {}
        assert result.confidence_tier == "low"


class TestMultilingual:
    def test_a_single_language_panel_is_extracted_and_its_rule_evaluated(self, db) -> None:
        """The baseline the other cases are measured against: a label whose
        language declaration is backed by a real token ("English" cites
        "en") verifies, and FSSAI's language-coverage rule genuinely
        evaluates it."""
        result = run_adversarial(db, adversarial_case(case_id="multilingual-english"))

        assert result.actual_fields["languages.detected"] == "en"
        assert result.verified_by_field["languages.detected"] is True
        assert result.actual_findings["IN-FSSAI-FOOD-LABEL-LANGUAGE-COMPLIANT"] == "pass"

    def test_a_bilingual_panel_records_both_languages_but_cannot_verify_the_list(
        self, db
    ) -> None:
        """A real, already-documented limitation surfaced rather than
        papered over: `app.extraction.evidence`'s own "SCOPE, STATED
        HONESTLY" note says list-shaped facts are verified as one joined
        string, not per item. A bilingual declaration therefore joins to
        "en; hi" and is compared against the joined token text, scoring far
        below the 0.85 gate - so both languages *are* extracted onto the
        row, but the field is demoted and the language rule receives
        `insufficient_data`.

        That is the safe direction to fail (review, never a silent pass),
        and it is exactly what section 23's Multilingual row does NOT fully
        get today. Recorded in TESTTEST.md as a stated limitation with the
        fix it needs (per-item `ExtractedField` rows), not asserted as if
        it worked.
        """
        extraction = baseline_extraction()
        extraction["languages_detected"] = {
            "values": ["en", "hi"],
            "not_found_reason": None,
            "token_ids": [10, 11],
            "confidence": 0.9,
        }

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="multilingual-bilingual",
                tokens=(*BASELINE_TOKENS, "Hindi"),
                extraction=extraction,
            ),
        )

        # Per-language extraction itself does work - both codes are on the
        # row, not just the first.
        assert result.actual_fields["languages.detected"] == "en; hi"
        # But the joined-list verification demotes it, so the rule is not
        # given data it cannot stand behind.
        assert result.verified_by_field["languages.detected"] is False
        assert result.actual_findings["IN-FSSAI-FOOD-LABEL-LANGUAGE-COMPLIANT"] == (
            "insufficient_data"
        )
        assert result.confidence_tier != "high"

    def test_a_label_in_neither_accepted_language_fails_the_language_rule(self, db) -> None:
        """The control proving the rule is genuinely evaluated rather than
        vacuously passing: a label declaring only French must fail FSSAI's
        English-or-Hindi requirement."""
        extraction = baseline_extraction()
        extraction["languages_detected"] = {
            "values": ["fr"],
            "not_found_reason": None,
            "token_ids": [10],
            "confidence": 0.9,
        }

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="multilingual-french-only",
                tokens=(*BASELINE_TOKENS[:10], "Francais"),
                extraction=extraction,
            ),
        )

        assert result.actual_findings["IN-FSSAI-FOOD-LABEL-LANGUAGE-COMPLIANT"] == "fail"
