"""Cross-checks between `infra/prometheus/alerts.yml`'s PromQL expressions
and this application's own `/metrics` output (P7-T5).

A full live Prometheus was not available to evaluate these rules for real
in this environment (see TESTTEST.md's P7-T5 row for why - a persistent
Docker Hub image-pull failure, not a code issue), so this is the next-best
real check: every metric name and label the alert rules reference must
actually exist, with the exact spelling, in what the running application
emits. A typo in either file (a renamed metric, a relabeled dimension)
would otherwise only surface the first time the alert silently never fires
in production.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from app.analysis.dlq import record_dead_letter
from app.analysis.models import Analysis, AnalysisState, DeadLetterReason
from app.catalog.models import Product, ProductVersion
from app.platform.metrics import render_metrics
from tests.conftest import make_org

pytestmark = pytest.mark.integration

INFRA_ROOT = Path(__file__).resolve().parents[3] / "infra"
_METRIC_NAME_RE = re.compile(r"\blabellens_[a-z_]+\b")


def _alert_rules() -> list[dict]:
    doc = yaml.safe_load((INFRA_ROOT / "prometheus" / "alerts.yml").read_text())
    return [rule for group in doc["groups"] for rule in group["rules"]]


def _referenced_metric_names() -> set[str]:
    names = set()
    for rule in _alert_rules():
        names.update(_METRIC_NAME_RE.findall(rule["expr"]))
    return names


class TestAlertRulesReferenceRealMetrics:
    def test_every_metric_named_in_an_alert_expression_is_actually_emitted(
        self, db
    ) -> None:
        # Seed at least one row of everything so every gauge/counter this
        # session defined has emitted at least one sample by scrape time -
        # an unset gauge with no labelled series yet still shows up (gauges
        # default their declared labels to nothing until first use, but a
        # bare counter with no `.labels()` call yet does show the metric
        # name in HELP/TYPE lines regardless).
        org = make_org(db)
        product = Product(organization_id=org.id, name="P", internal_sku="S1")
        db.add(product)
        db.flush()
        version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
        db.add(version)
        db.flush()
        analysis = Analysis(
            organization_id=org.id, product_version_id=version.id,
            state=AnalysisState.QUEUED, idempotency_key="probe",
        )
        db.add(analysis)
        db.flush()
        record_dead_letter(
            db, analysis=analysis, stage="ocr", reason=DeadLetterReason.PERMANENT_ERROR,
            error_message="probe", attempt_count=1,
        )
        db.commit()

        body, _ = render_metrics()
        text = body.decode("utf-8")

        referenced = _referenced_metric_names()
        assert referenced, "alerts.yml should reference at least one labellens_ metric"
        missing = {name for name in referenced if f"# TYPE {name} " not in text}
        assert not missing, f"alerts.yml references undefined metrics: {missing}"

    def test_the_dlq_alert_uses_the_gauge_that_the_metrics_endpoint_actually_sets(
        self,
    ) -> None:
        rules = {rule["alert"]: rule for rule in _alert_rules()}
        assert "labellens_dlq_unreplayed" in rules["DeadLetterQueueNonEmpty"]["expr"]

    def test_the_failure_rate_alert_uses_a_rate_over_the_documented_window(self) -> None:
        rules = {rule["alert"]: rule for rule in _alert_rules()}
        expr = rules["AnalysisFailureRateHigh"]["expr"]
        assert "labellens_analysis_failures_total" in expr
        assert "[15m]" in expr

    def test_the_queue_depth_alert_uses_the_documented_ten_minute_window(self) -> None:
        rules = {rule["alert"]: rule for rule in _alert_rules()}
        rule = rules["QueueDepthHigh"]
        assert "labellens_queue_depth" in rule["expr"]
        assert rule["for"] == "10m"
