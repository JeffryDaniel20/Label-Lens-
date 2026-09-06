"""Unit tests for the pure confidence-math half of P3-T8: the threshold
table and the field-confidence combinator. The DB-touching half
(`compute_analysis_tier`, aggregating real `ExtractedField`/`OcrTokenRow`
rows) has its own integration suite in
`tests/integration/test_confidence_tiers.py`.
"""

from __future__ import annotations

import pytest

from app.analysis.models import ConfidenceTier
from app.confidence.tiers import (
    HIGH_THRESHOLD,
    MEDIUM_THRESHOLD,
    field_confidence,
    tier_for_confidence,
)

pytestmark = pytest.mark.unit


class TestTierForConfidence:
    def test_the_high_threshold_is_inclusive(self) -> None:
        assert tier_for_confidence(HIGH_THRESHOLD) is ConfidenceTier.HIGH

    def test_just_below_the_high_threshold_is_medium(self) -> None:
        assert tier_for_confidence(HIGH_THRESHOLD - 0.0001) is ConfidenceTier.MEDIUM

    def test_the_medium_threshold_is_inclusive(self) -> None:
        assert tier_for_confidence(MEDIUM_THRESHOLD) is ConfidenceTier.MEDIUM

    def test_just_below_the_medium_threshold_is_low(self) -> None:
        assert tier_for_confidence(MEDIUM_THRESHOLD - 0.0001) is ConfidenceTier.LOW

    def test_zero_is_low(self) -> None:
        assert tier_for_confidence(0.0) is ConfidenceTier.LOW

    def test_a_perfect_score_is_high(self) -> None:
        assert tier_for_confidence(1.0) is ConfidenceTier.HIGH


class TestFieldConfidence:
    def test_is_the_minimum_of_ocr_and_extraction_confidence(self) -> None:
        assert field_confidence(0.95, 0.6) == 0.6
        assert field_confidence(0.6, 0.95) == 0.6

    def test_equal_inputs_pass_through(self) -> None:
        assert field_confidence(0.8, 0.8) == 0.8

    def test_a_weak_ocr_read_caps_a_confident_extraction(self) -> None:
        """A model can be very sure about a value it read off badly-scanned
        text - the OCR confidence must still cap the result, since the
        underlying text itself was unreliable."""
        assert field_confidence(0.5, 0.99) == 0.5
