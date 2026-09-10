"""Integration tests for `GET /metrics` (P7-T5): the live-query gauges
(analyses by state, DLQ unreplayed count) reflecting real rows, over the
real router. No auth - see the endpoint's own docstring for why.
"""

from __future__ import annotations

import uuid

import pytest

from app.analysis.dlq import record_dead_letter
from app.analysis.models import Analysis, AnalysisState, DeadLetterReason
from app.catalog.models import Product, ProductVersion
from tests.conftest import make_org

pytestmark = pytest.mark.integration


class TestMetricsEndpoint:
    def test_requires_no_authentication(self, client) -> None:
        response = client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]

    def test_reflects_a_real_analysis_and_a_real_dlq_arrival(self, client, db) -> None:
        org = make_org(db)
        product = Product(organization_id=org.id, name="P", internal_sku="S1")
        db.add(product)
        db.flush()
        version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
        db.add(version)
        db.flush()
        analysis = Analysis(
            organization_id=org.id,
            product_version_id=version.id,
            state=AnalysisState.QUEUED,
            idempotency_key=uuid.uuid4().hex,
        )
        db.add(analysis)
        db.flush()
        record_dead_letter(
            db, analysis=analysis, stage="ocr", reason=DeadLetterReason.PERMANENT_ERROR,
            error_message="boom", attempt_count=1,
        )
        db.commit()

        response = client.get("/metrics")
        assert response.status_code == 200
        body = response.text
        assert 'labellens_analyses_by_state{state="queued"}' in body
        assert "labellens_dlq_unreplayed " in body
        # At least one unreplayed DLQ row exists now - the gauge must be >= 1,
        # not stuck at its initial 0.
        for line in body.splitlines():
            if line.startswith("labellens_dlq_unreplayed "):
                assert float(line.split()[-1]) >= 1
                break
        else:
            pytest.fail("labellens_dlq_unreplayed not found in /metrics output")
