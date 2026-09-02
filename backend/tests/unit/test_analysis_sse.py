"""Unit tests for the SSE progress stream (P5-T5): the pure async generator
`stream_analysis_events`, driven directly with `asyncio.run` - no live HTTP
server needed to prove the polling/formatting/termination logic. The real
end-to-end wire format (over an actual `StreamingResponse`) is covered at
the router level in `tests/integration/test_analysis_sse_router.py`.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from app.analysis import service, sse
from app.analysis.models import AnalysisState
from app.analysis.stages import advance_analysis
from app.analysis.state_machine import transition
from app.catalog.models import File, FileStatus, Product, ProductVersion
from tests.conftest import make_org

pytestmark = pytest.mark.unit


@pytest.fixture
def analysis(db):
    org = make_org(db)
    product = Product(organization_id=org.id, name="P", internal_sku="S1")
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    db.add(
        File(
            organization_id=org.id,
            product_version_id=version.id,
            storage_key="k",
            original_filename="f.jpg",
            sha256="a" * 64,
            mime="image/jpeg",
            bytes=1,
            status=FileStatus.READY,
        )
    )
    db.flush()
    file_hash = service.compute_file_set_hash(db, organization_id=org.id, version_id=version.id)
    row, _ = service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return org, row


async def _collect(agen) -> list[str]:
    return [chunk async for chunk in agen]


class TestJsonDefault:
    def test_a_datetime_is_isoformatted(self) -> None:
        import datetime as dt

        value = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
        assert sse._json_default(value) == value.isoformat()

    def test_anything_else_raises(self) -> None:
        with pytest.raises(TypeError):
            sse._json_default(object())


class TestFormatSseEvent:
    def test_formats_id_event_and_data_lines(self, db, analysis) -> None:
        _org, row = analysis
        events = service.get_events(db, organization_id=row.organization_id, analysis_id=row.id)
        text = sse.format_sse_event(events[0], percentage=0)
        assert text.startswith("id: 1\n")
        assert "event: analysis.transition\n" in text
        assert text.endswith("\n\n")
        data_line = next(line for line in text.splitlines() if line.startswith("data: "))
        payload = json.loads(data_line[len("data: ") :])
        assert payload["sequence"] == 1
        assert payload["to_state"] == "queued"
        assert payload["percentage"] == 0


class TestStreamAnalysisEvents:
    def test_streams_every_transition_already_committed(self, db, analysis) -> None:
        org, row = analysis
        advance_analysis(db, row)  # queued -> validating
        db.commit()
        advance_analysis(db, row)  # validating -> preprocessing
        db.commit()

        chunks = asyncio.run(
            _collect(
                sse.stream_analysis_events(
                    db,
                    organization_id=org.id,
                    analysis_id=row.id,
                    poll_interval=0.01,
                    max_iterations=1,
                )
            )
        )
        # queued, validating, preprocessing - all already committed before
        # the stream started, so one poll sees all three and then... the
        # analysis isn't stopped, but max_iterations=1 caps it here anyway.
        assert len(chunks) == 3
        to_states = [json.loads(c.splitlines()[-2][len("data: ") :])["to_state"] for c in chunks]
        assert to_states == ["queued", "validating", "preprocessing"]

    def test_resuming_from_a_sequence_only_sends_whats_new(self, db, analysis) -> None:
        org, row = analysis
        advance_analysis(db, row)
        db.commit()

        chunks = asyncio.run(
            _collect(
                sse.stream_analysis_events(
                    db,
                    organization_id=org.id,
                    analysis_id=row.id,
                    since_sequence=1,  # already saw the "queued" event
                    poll_interval=0.01,
                    max_iterations=1,
                )
            )
        )
        assert len(chunks) == 1
        assert "validating" in chunks[0]

    def test_the_stream_closes_on_its_own_once_the_analysis_reaches_a_terminal_state(
        self, db, analysis
    ) -> None:
        org, row = analysis
        transition(db, row, AnalysisState.CANCELLED)
        db.commit()

        # No max_iterations needed: a terminal state ends the generator on
        # the very first poll, so this genuinely terminates on its own.
        chunks = asyncio.run(
            _collect(
                sse.stream_analysis_events(
                    db, organization_id=org.id, analysis_id=row.id, poll_interval=0.01
                )
            )
        )
        # Both the initial "queued" event and the "cancelled" transition are
        # new to this stream (since_sequence defaults to 0).
        assert len(chunks) == 2
        assert "cancelled" in chunks[-1]

    def test_the_stream_closes_once_awaiting_human_review(self, db, analysis) -> None:
        org, row = analysis
        for target in (
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.OCR,
            AnalysisState.EXTRACTING,
            AnalysisState.NORMALIZING,
            AnalysisState.CLASSIFYING,
            AnalysisState.RULE_EVAL,
            AnalysisState.SCORING,
            AnalysisState.NEEDS_REVIEW,
        ):
            transition(db, row, target)
        db.commit()

        chunks = asyncio.run(
            _collect(
                sse.stream_analysis_events(
                    db, organization_id=org.id, analysis_id=row.id, poll_interval=0.01
                )
            )
        )
        assert "needs_review" in chunks[-1]

    def test_a_stalled_stream_polls_again_and_eventually_stops_via_max_iterations(
        self, db, analysis
    ) -> None:
        org, row = analysis
        advance_analysis(db, row)  # queued -> validating
        db.commit()
        # No further transitions committed - the stream has nothing new to
        # report, so it should poll `max_iterations` times and then give up
        # rather than looping forever.
        chunks = asyncio.run(
            _collect(
                sse.stream_analysis_events(
                    db,
                    organization_id=org.id,
                    analysis_id=row.id,
                    since_sequence=2,  # already caught up to "validating"
                    poll_interval=0.01,
                    max_iterations=3,
                )
            )
        )
        assert chunks == []
