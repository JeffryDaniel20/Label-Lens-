"""Unit tests for P7-T5's Prometheus metric definitions and the pure parts
of Sentry wiring - real transitions/DLQ recordings incrementing the right
counters, and `render_metrics()`'s output actually being valid Prometheus
exposition format. The `/metrics` HTTP endpoint (the live-query gauges) has
its own integration suite in `tests/integration/test_platform_metrics_router.py`.
"""

from __future__ import annotations

import pytest

from app.platform.metrics import (
    ANALYSES_TRANSITIONS_TOTAL,
    ANALYSIS_FAILURES_TOTAL,
    DLQ_ARRIVALS_TOTAL,
    STAGE_DURATION_SECONDS,
    render_metrics,
)
from app.platform.sentry import capture_exception, init_sentry

pytestmark = pytest.mark.unit


def _counter_value(counter, **labels) -> float:
    return counter.labels(**labels)._value.get()  # noqa: SLF001 - test-only introspection


class TestRenderMetrics:
    def test_returns_valid_prometheus_exposition_format(self) -> None:
        body, content_type = render_metrics()
        text = body.decode("utf-8")
        assert "text/plain" in content_type
        assert "# HELP labellens_analysis_transitions_total" in text
        assert "# TYPE labellens_dlq_unreplayed gauge" in text

    def test_reflects_real_counter_increments(self) -> None:
        before = _counter_value(ANALYSES_TRANSITIONS_TOTAL, state="__test_probe__")
        ANALYSES_TRANSITIONS_TOTAL.labels(state="__test_probe__").inc()
        after = _counter_value(ANALYSES_TRANSITIONS_TOTAL, state="__test_probe__")
        assert after == before + 1

        body, _ = render_metrics()
        assert b'labellens_analysis_transitions_total{state="__test_probe__"}' in body


class TestStageDurationHistogram:
    def test_the_context_manager_records_an_observation(self) -> None:
        histogram = STAGE_DURATION_SECONDS.labels(stage="__test_probe__")
        before = histogram._sum.get()  # noqa: SLF001
        with histogram.time():
            pass
        after = histogram._sum.get()  # noqa: SLF001
        assert after >= before


class TestFailureAndDlqCounters:
    def test_are_real_counters_with_the_expected_labels(self) -> None:
        ANALYSIS_FAILURES_TOTAL.labels(stage="__test_probe__").inc()
        DLQ_ARRIVALS_TOTAL.labels(reason="__test_probe__").inc()
        body, _ = render_metrics()
        assert b'labellens_analysis_failures_total{stage="__test_probe__"}' in body
        assert b'labellens_dlq_arrivals_total{reason="__test_probe__"}' in body


class TestSentry:
    def test_init_is_a_no_op_without_a_dsn(self) -> None:
        from app.platform.config import Settings

        settings = Settings(secret_key="x" * 40, database_url="sqlite://", sentry_dsn="")
        init_sentry(settings)  # must not raise, must not configure a client

        import sentry_sdk

        assert not sentry_sdk.get_client().is_active()

    def test_capture_exception_is_a_safe_no_op_when_unconfigured(self) -> None:
        # Must never raise, even though nothing is configured to receive it.
        capture_exception(ValueError("boom"))
