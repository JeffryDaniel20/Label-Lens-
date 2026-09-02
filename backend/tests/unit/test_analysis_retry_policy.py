"""Unit tests for the pure retry/timeout policy (P5-T3): backoff, per-stage
timeouts, and the whole-analysis budget check. No DB or live services."""

from __future__ import annotations

import datetime as dt
import random

import pytest

from app.analysis import retry_policy
from app.analysis.models import AnalysisState

pytestmark = pytest.mark.unit


class TestBackoff:
    def test_backoff_grows_exponentially_before_the_cap(self) -> None:
        rng = random.Random(0)
        assert 1.0 <= retry_policy.backoff_seconds(0, rng=rng) < 1.5
        assert 2.0 <= retry_policy.backoff_seconds(1, rng=rng) < 3.0
        assert 4.0 <= retry_policy.backoff_seconds(2, rng=rng) < 6.0

    def test_backoff_is_capped(self) -> None:
        rng = random.Random(0)
        assert 60.0 <= retry_policy.backoff_seconds(20, rng=rng) < 90.0

    def test_jitter_makes_repeated_calls_differ(self) -> None:
        rng = random.Random(1)
        values = {retry_policy.backoff_seconds(3, rng=rng) for _ in range(10)}
        assert len(values) > 1

    def test_default_rng_is_usable_without_one_supplied(self) -> None:
        value = retry_policy.backoff_seconds(1)
        assert 2.0 <= value < 3.0


class TestStageTimeouts:
    def test_ocr_and_extracting_have_their_specified_timeouts(self) -> None:
        assert retry_policy.stage_timeout_seconds(AnalysisState.OCR) == 120.0
        assert retry_policy.stage_timeout_seconds(AnalysisState.EXTRACTING) == 90.0

    def test_every_other_stage_gets_the_default_timeout(self) -> None:
        for state in (
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.NORMALIZING,
            AnalysisState.CLASSIFYING,
            AnalysisState.RULE_EVAL,
            AnalysisState.SCORING,
        ):
            assert (
                retry_policy.stage_timeout_seconds(state)
                == retry_policy.DEFAULT_STAGE_TIMEOUT_SECONDS
            )


class TestAnalysisBudget:
    def test_within_budget_is_not_exceeded(self) -> None:
        started = dt.datetime.now(dt.UTC)
        now = started + dt.timedelta(minutes=5)
        assert retry_policy.is_budget_exceeded(started, now=now) is False

    def test_past_budget_is_exceeded(self) -> None:
        started = dt.datetime.now(dt.UTC)
        now = started + dt.timedelta(minutes=21)
        assert retry_policy.is_budget_exceeded(started, now=now) is True

    def test_exactly_at_the_boundary_is_not_yet_exceeded(self) -> None:
        started = dt.datetime.now(dt.UTC)
        now = started + dt.timedelta(seconds=retry_policy.ANALYSIS_BUDGET_SECONDS)
        assert retry_policy.is_budget_exceeded(started, now=now) is False

    def test_a_naive_started_at_is_treated_as_utc(self) -> None:
        # SQLite (the test suite's default engine) hands back a naive
        # datetime even for a `DateTime(timezone=True)` column.
        started = dt.datetime.now(dt.UTC).replace(tzinfo=None)
        now = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=21)
        assert retry_policy.is_budget_exceeded(started, now=now) is True


class TestExceptionHierarchy:
    def test_stage_timeout_error_is_a_transient_stage_error(self) -> None:
        assert issubclass(retry_policy.StageTimeoutError, retry_policy.TransientStageError)

    def test_transient_and_permanent_are_distinct(self) -> None:
        assert not issubclass(
            retry_policy.TransientStageError, retry_policy.PermanentStageError
        )
        assert not issubclass(
            retry_policy.PermanentStageError, retry_policy.TransientStageError
        )
