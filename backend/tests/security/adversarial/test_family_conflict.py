"""Section 23's "Conflict" family.

Expected behaviour, verbatim: *conflicting-evidence finding + mandatory
review.*

The **mandatory review** half is real as of P7-T4: `app.extraction.conflicts`
detects a field whose own cited OCR tokens disagree under that field's
normalizer, and `app.confidence.tiers` forces it to Low, which routes the
analysis to review. Before this task, `tiers.py`'s own routing table
promised exactly this ("or conflicting duplicates -> mandatory review") and
nothing implemented it - the tests below are what makes the promise true.

The **conflicting-evidence finding** half is deliberately NOT implemented,
and this is a design decision rather than an omission: findings are the rule
engine's output and nothing else in this system is allowed to manufacture
one ("AI extracts, rules decide" - IMPLEMENTATION.md section 1). Emitting a
synthetic `Finding` row from the confidence layer would put a compliance
verdict outside the rule engine for the first time, to say something the
tier already says. If a jurisdiction wants a conflicting-declaration rule,
it belongs in that jurisdiction's pack as a real, cited rule. Recorded in
TESTTEST.md as a stated limitation.

See `app.extraction.conflicts` for which conflict shape is detectable
without false positives and which is not.
"""

from __future__ import annotations

import pytest

from app.extraction.conflicts import canonicalize, find_conflicting_citation
from tests.security.adversarial.rig import (
    BASELINE_TOKENS,
    adversarial_case,
    baseline_extraction,
    run_adversarial,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]


class TestConflictDetectorItself:
    """Pure-function behaviour, hand-checked - no DB, no pipeline."""

    def test_a_single_citation_can_never_conflict(self) -> None:
        assert find_conflicting_citation("quantity.net_quantity", ["250 g"]) is None

    def test_two_citations_with_the_same_value_are_not_a_conflict(self) -> None:
        assert find_conflicting_citation("quantity.net_quantity", ["250 g", "250 g"]) is None

    def test_the_same_quantity_in_different_units_is_not_a_conflict(self) -> None:
        """"0.25 kg" and "250 g" are the same declaration printed twice, not
        a contradiction - which is exactly why this compares canonical
        values instead of raw strings."""
        assert find_conflicting_citation("quantity.net_quantity", ["250 g", "0.25 kg"]) is None

    def test_two_genuinely_different_quantities_are_a_conflict(self) -> None:
        reason = find_conflicting_citation("quantity.net_quantity", ["250 g", "500 g"])
        assert reason is not None
        assert "250 g" in reason and "500 g" in reason

    def test_two_genuinely_different_dates_are_a_conflict(self) -> None:
        reason = find_conflicting_citation(
            "dates.expiry_or_best_before", ["12/2027", "01/2028"]
        )
        assert reason is not None

    def test_the_same_date_in_two_formats_is_not_a_conflict(self) -> None:
        assert (
            find_conflicting_citation("dates.expiry_or_best_before", ["2027-12", "12/2027"])
            is None
        )

    def test_an_unparseable_citation_is_ignored_not_treated_as_a_conflict(self) -> None:
        """A token the normalizer has no opinion about is not evidence of
        disagreement - the alternative would flag every field citing a
        label-text token alongside its value."""
        assert (
            find_conflicting_citation("quantity.net_quantity", ["250 g", "NET WT."]) is None
        )

    def test_a_field_with_no_normalizer_is_never_flagged(self) -> None:
        assert (
            find_conflicting_citation(
                "ingredients.declared_text", ["Sugar, Wheat", "Salt, Cocoa"]
            )
            is None
        )

    def test_canonicalize_has_no_opinion_about_unknown_fields(self) -> None:
        assert canonicalize("ingredients.declared_text", "250 g") is None
        assert canonicalize("quantity.net_quantity", "250 g") == "250g"


class TestConflictRoutesToMandatoryReview:
    def test_two_different_net_weights_cited_for_one_field_force_review(self, db) -> None:
        extraction = baseline_extraction()
        # The packet prints two different net weights and the model cites
        # both for the one net-quantity field.
        extraction["quantity_net_quantity"] = {
            "value": "250 g",
            "not_found_reason": None,
            "token_ids": [1, 11],
            "confidence": 0.99,
        }

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="conflict-net-weight",
                tokens=(*BASELINE_TOKENS, "500 g"),
                extraction=extraction,
                token_confidence=0.99,
            ),
        )

        assert result.confidence_tier == "low", (
            "a field whose own citations disagree must force mandatory review"
        )

    def test_two_different_expiry_dates_cited_for_one_field_force_review(self, db) -> None:
        extraction = baseline_extraction()
        extraction["dates_expiry_or_best_before"] = {
            "value": "12/2027",
            "not_found_reason": None,
            "token_ids": [3, 11],
            "confidence": 0.95,
        }

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="conflict-expiry",
                tokens=(*BASELINE_TOKENS, "01/2028"),
                extraction=extraction,
                token_confidence=0.99,
            ),
        )

        assert result.confidence_tier == "low"

    def test_the_same_value_cited_twice_does_not_force_review(self, db) -> None:
        """The control: citing a value printed twice on the packet (front
        and back, say) is normal and must NOT be treated as a conflict, or
        the signal is worthless."""
        extraction = baseline_extraction()
        extraction["quantity_net_quantity"] = {
            "value": "250 g",
            "not_found_reason": None,
            "token_ids": [1, 11],
            "confidence": 0.99,
        }

        result = run_adversarial(
            db,
            adversarial_case(
                case_id="conflict-control-same-value",
                tokens=(*BASELINE_TOKENS, "0.25 kg"),
                extraction=extraction,
                token_confidence=0.99,
            ),
        )

        assert result.confidence_tier == "high"
