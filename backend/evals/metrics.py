"""Pure metric math for the eval harness (P7-T3, IMPLEMENTATION.md section
22's own metric list). Every function here takes already-computed
expected/actual values and returns a number - no DB, no pipeline stage, no
I/O - so the arithmetic is independently verifiable against hand-computed
expected results (`tests/unit/test_eval_metrics.py`) regardless of whether
the pipeline feeding it is itself correct.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class PrecisionRecallF1:
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float | None:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else None

    @property
    def recall(self) -> float | None:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else None

    @property
    def f1(self) -> float | None:
        """`None` only when precision or recall itself is undefined (no
        assertions, or no ground truth, to judge against at all) - a
        completely-wrong prediction (`precision == recall == 0.0`) is a
        real, defined outcome, `0.0`, not "undefined"."""
        p, r = self.precision, self.recall
        if p is None or r is None:
            return None
        if p + r == 0:
            return 0.0
        return 2 * p * r / (p + r)


def field_extraction_prf1(
    expected: Mapping[str, object | None], actual: Mapping[str, object | None]
) -> PrecisionRecallF1:
    """Exact-match precision/recall/F1 (IMPLEMENTATION.md section 22's
    "exact-match after normalization; fuzzy variant reported separately" -
    this harness compares `ExtractedField.value_raw`, stated honestly in
    `evals.dataset`'s own docstring as pre-normalization exact-match, not
    the fully-normalized structured value).

    A field path both sides agree is absent is a true negative - not scored
    at all, the conventional definition of precision/recall over positive
    assertions only. A wrongly-valued field (both sides non-null, but
    different) counts against BOTH precision (a wrong assertion was made)
    and recall (the real value was missed) - it is not "half correct."
    """
    true_positives = false_positives = false_negatives = 0
    for path in expected.keys() | actual.keys():
        expected_value = expected.get(path)
        actual_value = actual.get(path)
        if expected_value is None and actual_value is None:
            continue
        is_match = (
            expected_value is not None
            and actual_value is not None
            and expected_value == actual_value
        )
        if is_match:
            true_positives += 1
            continue
        if actual_value is not None:
            false_positives += 1
        if expected_value is not None:
            false_negatives += 1
    return PrecisionRecallF1(true_positives, false_positives, false_negatives)


def finding_fail_detection_prf1(
    expected: Mapping[str, str], actual: Mapping[str, str]
) -> PrecisionRecallF1:
    """Finding-level precision/recall (section 22) framed around the
    safety-critical question: did this case's real compliance violations
    (a `fail` verdict) get correctly flagged? A rule key present on only one
    side is not scored - both sides must have judged that rule for its
    verdict to be comparable at all."""
    true_positives = false_positives = false_negatives = 0
    for rule_key in expected.keys() & actual.keys():
        expected_fail = expected[rule_key] == "fail"
        actual_fail = actual[rule_key] == "fail"
        if expected_fail and actual_fail:
            true_positives += 1
        elif actual_fail and not expected_fail:
            false_positives += 1
        elif expected_fail and not actual_fail:
            false_negatives += 1
    return PrecisionRecallF1(true_positives, false_positives, false_negatives)


def finding_status_accuracy(
    expected: Mapping[str, str], actual: Mapping[str, str]
) -> float | None:
    """The simpler multi-class complement to `finding_fail_detection_prf1`:
    what fraction of rules this case judges did the pipeline resolve to
    exactly the expected status (`pass`/`fail`/`insufficient_data`/
    `not_applicable`), not just "flagged a violation or not". `None` when
    neither side judged any common rule - nothing to score."""
    common_keys = expected.keys() & actual.keys()
    if not common_keys:
        return None
    correct = sum(1 for key in common_keys if expected[key] == actual[key])
    return correct / len(common_keys)


def hallucination_rate(verified_flags: Sequence[bool]) -> float | None:
    """Section 22's "fields asserted with no supporting evidence": the
    fraction of every field the model asserted a non-null value for
    (`verified_flags` already filtered to those) whose citation did NOT
    check out against `app.extraction.evidence.verify_extraction` - `False`
    entries. `None` when nothing was asserted at all - a case with zero
    assertions has no hallucination rate to report, not a rate of 0."""
    if not verified_flags:
        return None
    unsupported = sum(1 for verified in verified_flags if not verified)
    return unsupported / len(verified_flags)


@dataclass(frozen=True, slots=True)
class CalibrationBin:
    lower: float
    upper: float
    count: int
    avg_confidence: float | None
    accuracy: float | None


def calibration_curve(
    points: Sequence[tuple[float, bool]], *, n_bins: int = 5
) -> list[CalibrationBin]:
    """Section 22's "reliability curve": each point is (predicted
    confidence, was the prediction actually correct). Bins are fixed-width
    over `[0, 1]`; the last bin is inclusive of `1.0` on both ends so a
    perfectly confident correct prediction lands somewhere, not nowhere."""
    width = 1.0 / n_bins
    bins: list[CalibrationBin] = []
    for i in range(n_bins):
        lower, upper = i * width, (i + 1) * width
        in_bin = [
            (confidence, correct)
            for confidence, correct in points
            if (lower <= confidence < upper) or (i == n_bins - 1 and confidence == upper)
        ]
        if not in_bin:
            bins.append(CalibrationBin(lower, upper, 0, None, None))
            continue
        avg_confidence = sum(c for c, _ in in_bin) / len(in_bin)
        accuracy = sum(1 for _, correct in in_bin if correct) / len(in_bin)
        bins.append(CalibrationBin(lower, upper, len(in_bin), avg_confidence, accuracy))
    return bins


def expected_calibration_error(bins: Sequence[CalibrationBin]) -> float | None:
    """ECE: the count-weighted average gap between each bin's average
    confidence and its observed accuracy. `None` when every bin is empty."""
    total = sum(b.count for b in bins)
    if total == 0:
        return None
    return sum(
        (b.count / total) * abs((b.accuracy or 0.0) - (b.avg_confidence or 0.0))
        for b in bins
        if b.count > 0
    )
