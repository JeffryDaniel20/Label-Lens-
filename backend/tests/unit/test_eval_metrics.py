"""Pure metric math for the golden-dataset eval harness (P7-T3) - every
number here is hand-computed and asserted directly, independent of the
pipeline `evals.runner` drives, so a bug in the pipeline can never masquerade
as a bug in the arithmetic (or vice versa).
"""

from __future__ import annotations

import pytest

from evals.metrics import (
    calibration_curve,
    expected_calibration_error,
    field_extraction_prf1,
    finding_fail_detection_prf1,
    finding_status_accuracy,
    hallucination_rate,
)

pytestmark = pytest.mark.unit


class TestFieldExtractionPrf1:
    def test_perfect_match_is_precision_and_recall_1(self) -> None:
        result = field_extraction_prf1({"a": "x", "b": "y"}, {"a": "x", "b": "y"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            2,
            0,
            0,
        )
        assert result.precision == 1.0
        assert result.recall == 1.0
        assert result.f1 == 1.0

    def test_a_missed_field_is_a_false_negative_not_a_false_positive(self) -> None:
        result = field_extraction_prf1({"a": "x"}, {})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            0,
            1,
        )
        assert result.precision is None  # nothing was asserted at all
        assert result.recall == 0.0

    def test_a_hallucinated_field_is_a_false_positive_not_a_false_negative(self) -> None:
        result = field_extraction_prf1({}, {"a": "y"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            1,
            0,
        )
        assert result.precision == 0.0
        assert result.recall is None  # ground truth expected nothing here

    def test_a_wrong_value_counts_against_both_precision_and_recall(self) -> None:
        result = field_extraction_prf1({"a": "x"}, {"a": "y"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            1,
            1,
        )
        assert result.precision == 0.0
        assert result.recall == 0.0
        assert result.f1 == 0.0

    def test_both_sides_null_is_a_true_negative_not_scored(self) -> None:
        result = field_extraction_prf1({"a": None}, {"a": None})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            0,
            0,
        )
        assert result.precision is None
        assert result.recall is None
        assert result.f1 is None


class TestFindingFailDetectionPrf1:
    def test_a_correctly_flagged_violation_is_a_true_positive(self) -> None:
        result = finding_fail_detection_prf1({"R1": "fail"}, {"R1": "fail"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            1,
            0,
            0,
        )

    def test_a_missed_violation_is_a_false_negative(self) -> None:
        result = finding_fail_detection_prf1({"R1": "fail"}, {"R1": "pass"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            0,
            1,
        )

    def test_a_false_alarm_is_a_false_positive(self) -> None:
        result = finding_fail_detection_prf1({"R1": "pass"}, {"R1": "fail"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            1,
            0,
        )

    def test_agreeing_on_pass_is_not_scored_either_way(self) -> None:
        result = finding_fail_detection_prf1({"R1": "pass"}, {"R1": "pass"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            0,
            0,
        )
        assert result.precision is None
        assert result.recall is None

    def test_a_rule_key_judged_on_only_one_side_is_never_compared(self) -> None:
        result = finding_fail_detection_prf1({"R1": "fail"}, {"R2": "fail"})
        assert (result.true_positives, result.false_positives, result.false_negatives) == (
            0,
            0,
            0,
        )


class TestFindingStatusAccuracy:
    def test_exact_multiclass_match_rate(self) -> None:
        expected = {"R1": "pass", "R2": "fail", "R3": "insufficient_data"}
        actual = {"R1": "pass", "R2": "pass", "R3": "insufficient_data"}
        assert finding_status_accuracy(expected, actual) == pytest.approx(2 / 3)

    def test_no_common_rule_keys_is_none(self) -> None:
        assert finding_status_accuracy({"R1": "pass"}, {"R2": "pass"}) is None


class TestHallucinationRate:
    def test_fraction_of_asserted_fields_that_failed_verification(self) -> None:
        assert hallucination_rate([True, True, False, True]) == pytest.approx(0.25)

    def test_no_assertions_at_all_is_none(self) -> None:
        assert hallucination_rate([]) is None

    def test_all_verified_is_zero(self) -> None:
        assert hallucination_rate([True, True]) == 0.0


class TestCalibrationCurve:
    def test_points_land_in_the_correct_bin_with_correct_stats(self) -> None:
        points = [(0.1, True), (0.15, False), (0.9, True), (0.95, True)]
        bins = calibration_curve(points, n_bins=5)
        assert len(bins) == 5
        low_bin = bins[0]
        assert low_bin.lower == 0.0 and low_bin.upper == 0.2
        assert low_bin.count == 2
        assert low_bin.avg_confidence == pytest.approx(0.125)
        assert low_bin.accuracy == pytest.approx(0.5)
        high_bin = bins[4]
        assert high_bin.count == 2
        assert high_bin.accuracy == 1.0

    def test_a_confidence_of_exactly_1_lands_in_the_last_bin(self) -> None:
        bins = calibration_curve([(1.0, True)], n_bins=5)
        assert bins[4].count == 1

    def test_empty_points_produce_empty_bins(self) -> None:
        bins = calibration_curve([], n_bins=5)
        assert all(b.count == 0 for b in bins)
        assert all(b.avg_confidence is None and b.accuracy is None for b in bins)


class TestExpectedCalibrationError:
    def test_ece_is_the_count_weighted_gap_between_confidence_and_accuracy(self) -> None:
        # Bin [0.8, 1.0): both points correct (accuracy=1.0) at avg
        # confidence 0.9 - gap 0.1, weight 2/4. Bin [0.0, 0.2): both points
        # wrong (accuracy=0.0) at avg confidence 0.1 - gap 0.1, weight 2/4.
        # ECE = 0.5*0.1 + 0.5*0.1 = 0.1 (each bin's OWN confidence compared
        # to its OWN accuracy, never cross-bin).
        points = [(0.9, True), (0.9, True), (0.1, False), (0.1, False)]
        bins = calibration_curve(points, n_bins=5)
        assert expected_calibration_error(bins) == pytest.approx(0.1)

    def test_a_badly_miscalibrated_bin_has_a_large_ece(self) -> None:
        # One bin, confidence 0.9, but every prediction in it is wrong
        # (accuracy 0.0) - gap 0.9, the whole bin's own weight.
        points = [(0.9, False), (0.9, False)]
        bins = calibration_curve(points, n_bins=5)
        assert expected_calibration_error(bins) == pytest.approx(0.9)

    def test_no_data_at_all_is_none(self) -> None:
        bins = calibration_curve([], n_bins=5)
        assert expected_calibration_error(bins) is None
