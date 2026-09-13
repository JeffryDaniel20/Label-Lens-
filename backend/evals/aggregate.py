"""Combines every case's `evals.runner.CaseResult` into the one overall
`EvalSummary` section 22 asks for - pure aggregation over already-computed
per-case results, so it is unit-testable (`tests/unit/test_eval_aggregate.py`)
without running the pipeline at all.
"""

from __future__ import annotations

from dataclasses import dataclass

from evals.metrics import (
    CalibrationBin,
    PrecisionRecallF1,
    calibration_curve,
    expected_calibration_error,
    field_extraction_prf1,
    finding_fail_detection_prf1,
    hallucination_rate,
)
from evals.runner import CaseResult


@dataclass(frozen=True, slots=True)
class EvalSummary:
    case_count: int
    field_prf1: PrecisionRecallF1
    finding_prf1: PrecisionRecallF1
    finding_status_accuracy: float | None
    hallucination_rate: float | None
    human_review_rate: float | None
    critical_rule_false_negative_rate: float | None
    override_rate: None
    override_rate_note: str
    calibration_bins: list[CalibrationBin]
    expected_calibration_error: float | None


def aggregate(
    results: list[CaseResult], *, critical_rule_keys: frozenset[str]
) -> EvalSummary:
    field_tp = field_fp = field_fn = 0
    finding_tp = finding_fp = finding_fn = 0
    status_correct = status_total = 0
    all_verified_flags: list[bool] = []
    review_count = 0
    critical_expected_fail = 0
    critical_false_negatives = 0
    calibration_points: list[tuple[float, bool]] = []

    for result in results:
        field_result = field_extraction_prf1(result.expected_fields, result.actual_fields)
        field_tp += field_result.true_positives
        field_fp += field_result.false_positives
        field_fn += field_result.false_negatives

        finding_result = finding_fail_detection_prf1(
            result.expected_findings, result.actual_findings
        )
        finding_tp += finding_result.true_positives
        finding_fp += finding_result.false_positives
        finding_fn += finding_result.false_negatives

        common_keys = result.expected_findings.keys() & result.actual_findings.keys()
        status_correct += sum(
            1
            for key in common_keys
            if result.expected_findings[key] == result.actual_findings[key]
        )
        status_total += len(common_keys)

        all_verified_flags.extend(result.verified_flags)

        if result.confidence_tier != "high":
            review_count += 1

        for rule_key in critical_rule_keys & result.expected_findings.keys():
            if result.expected_findings[rule_key] == "fail":
                critical_expected_fail += 1
                if result.actual_findings.get(rule_key) != "fail":
                    critical_false_negatives += 1

        for rule_key, expected_status in result.expected_findings.items():
            actual_status = result.actual_findings.get(rule_key)
            confidence = result.finding_confidences.get(rule_key)
            if actual_status is None or confidence is None:
                continue
            calibration_points.append((confidence, actual_status == expected_status))

    bins = calibration_curve(calibration_points)

    return EvalSummary(
        case_count=len(results),
        field_prf1=PrecisionRecallF1(field_tp, field_fp, field_fn),
        finding_prf1=PrecisionRecallF1(finding_tp, finding_fp, finding_fn),
        finding_status_accuracy=(status_correct / status_total) if status_total else None,
        hallucination_rate=hallucination_rate(all_verified_flags),
        human_review_rate=(review_count / len(results)) if results else None,
        critical_rule_false_negative_rate=(
            critical_false_negatives / critical_expected_fail
            if critical_expected_fail
            else None
        ),
        override_rate=None,
        override_rate_note=(
            "Not applicable: this harness runs no real reviewer session, so there is no "
            "override to measure. Override rate is a live-production metric "
            "(app.platform.metrics), not something a golden-dataset eval run can produce."
        ),
        calibration_bins=bins,
        expected_calibration_error=expected_calibration_error(bins),
    )
