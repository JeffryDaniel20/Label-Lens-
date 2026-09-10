"""Unit tests for the pipeline stage sequence and chained advancement
(P5-T2). No live services needed - `advance_analysis()` only needs a
SQLAlchemy session (the shared `db` fixture, SQLite-backed).
"""

from __future__ import annotations

import pytest

from app.analysis.models import Analysis, AnalysisState
from app.analysis.stages import (
    DEFAULT_NEXT_STATE,
    STAGE_FUNCTIONS,
    STAGE_SEQUENCE,
    STOPPING_STATES,
    advance_analysis,
    progress_percentage,
    progress_percentage_for_state,
)
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
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
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="k",
        original_filename="f.jpg",
        sha256="a" * 64,
        mime="image/jpeg",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    # `_validating` (real, not a placeholder - see the module docstring)
    # needs a rasterized page to find, exactly like ingestion would have
    # already produced one for a real `ready` file.
    db.add(
        FilePage(
            organization_id=org.id, file_id=file.id, page_no=1, width=10, height=10, render_key="r"
        )
    )
    db.flush()
    row = Analysis(
        organization_id=org.id,
        product_version_id=version.id,
        state=AnalysisState.QUEUED,
        idempotency_key="x" * 64,
    )
    db.add(row)
    db.flush()
    return row


@pytest.fixture
def instrumented_stages(monkeypatch):
    """Replaces every registered stage function with one that records its
    own call, leaving the resumption/chaining logic under test - not any
    particular stage's real (still-blocked) business logic."""
    calls: list[str] = []
    original = dict(STAGE_FUNCTIONS)
    for state in list(STAGE_FUNCTIONS):

        def make(name: str):
            def fn(db, analysis):
                calls.append(name)
                return None

            return fn

        monkeypatch.setitem(STAGE_FUNCTIONS, state, make(state.value))
    yield calls
    STAGE_FUNCTIONS.clear()
    STAGE_FUNCTIONS.update(original)


class TestStageSequenceStructure:
    def test_default_next_state_covers_every_state(self) -> None:
        assert set(DEFAULT_NEXT_STATE.keys()) == set(AnalysisState) - STOPPING_STATES

    def test_stage_functions_are_registered_for_every_real_stage(self) -> None:
        assert set(STAGE_FUNCTIONS.keys()) == set(STAGE_SEQUENCE)

    def test_the_sequence_matches_implementation_mds_diagram(self) -> None:
        assert STAGE_SEQUENCE == (
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
            AnalysisState.OCR,
            AnalysisState.EXTRACTING,
            AnalysisState.EVIDENCE_VERIFICATION,
            AnalysisState.NORMALIZING,
            AnalysisState.CLASSIFYING,
            AnalysisState.RULE_EVAL,
            AnalysisState.SCORING,
        )

    def test_scorings_default_successor_is_completed(self) -> None:
        assert DEFAULT_NEXT_STATE[AnalysisState.SCORING] is AnalysisState.COMPLETED


class TestPlaceholderStages:
    def test_the_real_unpatched_validating_stage_passes_with_a_real_page(
        self, db, analysis
    ) -> None:
        """No monkeypatching here: exercises the genuine, currently-registered
        `_validating` (real, not a placeholder) against a fixture that has a
        real rasterized page - the success path a properly-ingested file
        actually takes."""
        advance_analysis(db, analysis)  # queued -> validating (no stage fn on queued)
        db.commit()
        result = advance_analysis(db, analysis)  # validating -> preprocessing (real _validating)
        db.commit()
        assert result is AnalysisState.PREPROCESSING

    def test_the_real_unpatched_preprocessing_placeholder_advances_without_doing_anything(
        self, db, analysis
    ) -> None:
        """`preprocessing` is the one stage in `STAGE_SEQUENCE` still a
        genuine placeholder (see the module docstring for why: `run_ocr`
        already calls `preprocess()` itself, so a separate pass would be
        redundant, not blocked)."""
        advance_analysis(db, analysis)  # queued -> validating
        db.commit()
        advance_analysis(db, analysis)  # validating -> preprocessing (real _validating)
        db.commit()
        result = advance_analysis(db, analysis)  # preprocessing -> ocr (real placeholder)
        db.commit()
        assert result is AnalysisState.OCR


class TestAdvanceAnalysisChaining:
    def test_a_full_run_advances_through_every_stage_exactly_once(
        self, db, analysis, instrumented_stages
    ) -> None:
        calls = instrumented_stages
        for _ in range(len(STAGE_SEQUENCE) + 1):  # +1 for queued -> validating
            advance_analysis(db, analysis)
            db.commit()
        assert analysis.state is AnalysisState.COMPLETED
        assert calls == [s.value for s in STAGE_SEQUENCE]

    def test_a_stopped_analysis_is_a_no_op(self, db, analysis, instrumented_stages) -> None:
        calls = instrumented_stages
        analysis.state = AnalysisState.COMPLETED
        db.flush()
        result = advance_analysis(db, analysis)
        assert result is AnalysisState.COMPLETED
        assert calls == []  # nothing was called; nothing new was recorded

    def test_needs_review_also_stops_automatic_advancement(
        self, db, analysis, instrumented_stages
    ) -> None:
        analysis.state = AnalysisState.NEEDS_REVIEW
        db.flush()
        result = advance_analysis(db, analysis)
        assert result is AnalysisState.NEEDS_REVIEW

    def test_a_state_with_no_default_next_state_is_a_safe_no_op(
        self, db, analysis, monkeypatch
    ) -> None:
        # A defensive branch: if a non-stopping state somehow has no
        # registered successor, advance_analysis must not crash or silently
        # invent a transition - it leaves the analysis exactly where it was.
        monkeypatch.delitem(DEFAULT_NEXT_STATE, AnalysisState.VALIDATING)
        advance_analysis(db, analysis)  # queued -> validating
        db.commit()
        result = advance_analysis(db, analysis)  # no successor registered
        assert result is AnalysisState.VALIDATING


class TestCheckpointingAndResumption:
    """The literal acceptance criterion: a killed worker mid-stage resumes
    without redoing completed stages."""

    def test_a_worker_crash_mid_stage_does_not_change_state(
        self, db, analysis, monkeypatch
    ) -> None:
        def boom(db, analysis):
            raise RuntimeError("simulated worker crash mid-stage")

        monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.VALIDATING, boom)
        advance_analysis(db, analysis)  # queued -> validating (no stage fn yet)
        db.commit()
        assert analysis.state is AnalysisState.VALIDATING

        with pytest.raises(RuntimeError):
            advance_analysis(db, analysis)
        db.rollback()
        assert analysis.state is AnalysisState.VALIDATING  # unchanged - never partially moved

    def test_resuming_after_a_crash_retries_only_the_failed_stage(
        self, db, analysis, monkeypatch
    ) -> None:
        calls: list[str] = []

        def track(name: str):
            def fn(db, analysis):
                calls.append(name)
                return None

            return fn

        crashed_once = {"done": False}

        def flaky(db, analysis):
            if not crashed_once["done"]:
                crashed_once["done"] = True
                raise RuntimeError("simulated worker crash mid-stage")
            calls.append("ocr")
            return None

        monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.VALIDATING, track("validating"))
        monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.PREPROCESSING, track("preprocessing"))
        monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.OCR, flaky)

        # Advance through queued -> validating -> preprocessing -> (about to run ocr).
        for _ in range(3):
            advance_analysis(db, analysis)
            db.commit()
        assert analysis.state is AnalysisState.OCR
        assert calls == ["validating", "preprocessing"]

        # The ocr stage crashes; the caller (a real worker task) rolls back.
        with pytest.raises(RuntimeError):
            advance_analysis(db, analysis)
        db.rollback()
        assert analysis.state is AnalysisState.OCR
        assert calls == ["validating", "preprocessing"]  # ocr not recorded as having run

        # "The worker restarts" and the same job is retried.
        new_state = advance_analysis(db, analysis)
        db.commit()
        assert new_state is AnalysisState.EXTRACTING
        # validating/preprocessing were NOT redone; ocr ran exactly once (its retry).
        assert calls == ["validating", "preprocessing", "ocr"]

    def test_events_show_no_gap_or_duplicate_across_the_crash(
        self, db, analysis, monkeypatch
    ) -> None:
        from app.analysis import service

        crashed_once = {"done": False}

        def flaky(db, analysis):
            if not crashed_once["done"]:
                crashed_once["done"] = True
                raise RuntimeError("simulated worker crash mid-stage")
            return None

        monkeypatch.setitem(STAGE_FUNCTIONS, AnalysisState.VALIDATING, flaky)
        advance_analysis(db, analysis)  # queued -> validating
        db.commit()

        with pytest.raises(RuntimeError):
            advance_analysis(db, analysis)  # crashes attempting validating -> preprocessing
        db.rollback()

        advance_analysis(db, analysis)  # retried, succeeds
        db.commit()

        events = service.get_events(
            db, organization_id=analysis.organization_id, analysis_id=analysis.id
        )
        # This fixture constructs `analysis` directly rather than through
        # `service.create_or_get_analysis()`, so there is no separate
        # "created in queued" event here - only the two real transitions.
        # Exactly 2 events (queued->validating, validating->preprocessing):
        # the failed attempt left no partial/duplicate event behind, and
        # sequence numbers are contiguous.
        assert [e.sequence for e in events] == [1, 2]
        assert [e.to_state for e in events] == [
            AnalysisState.VALIDATING,
            AnalysisState.PREPROCESSING,
        ]


class TestProgressPercentage:
    def test_queued_is_zero_and_completed_is_a_hundred(self) -> None:
        assert progress_percentage_for_state(AnalysisState.QUEUED) == 0
        assert progress_percentage_for_state(AnalysisState.COMPLETED) == 100

    def test_percentage_increases_monotonically_through_the_pipeline(self) -> None:
        states = (
            AnalysisState.QUEUED,
            *STAGE_SEQUENCE,
            AnalysisState.COMPLETED,
        )
        percentages = [progress_percentage_for_state(s) for s in states]
        assert percentages == sorted(percentages)
        assert len(set(percentages)) == len(percentages)  # each stage is distinct

    def test_needs_review_and_review_report_the_same_percentage_as_scoring(self) -> None:
        scoring = progress_percentage_for_state(AnalysisState.SCORING)
        assert progress_percentage_for_state(AnalysisState.NEEDS_REVIEW) == scoring
        assert progress_percentage_for_state(AnalysisState.REVIEW) == scoring

    def test_a_bare_failed_or_cancelled_state_reports_zero(self) -> None:
        assert progress_percentage_for_state(AnalysisState.FAILED) == 0
        assert progress_percentage_for_state(AnalysisState.CANCELLED) == 0

    def test_a_failed_analysis_reports_how_far_it_got(self, db, analysis) -> None:
        advance_analysis(db, analysis)  # queued -> validating
        db.commit()
        advance_analysis(db, analysis)  # validating -> preprocessing
        db.commit()
        from app.analysis.state_machine import transition

        transition(
            db, analysis, AnalysisState.FAILED, failure_stage="preprocessing", retryable=False
        )
        db.commit()

        assert progress_percentage(analysis) == progress_percentage_for_state(
            AnalysisState.PREPROCESSING
        )
        assert progress_percentage(analysis) != 0

    def test_a_cancelled_analysis_reports_zero_no_failure_stage_recorded(
        self, db, analysis
    ) -> None:
        advance_analysis(db, analysis)  # queued -> validating
        db.commit()
        from app.analysis.state_machine import transition

        transition(db, analysis, AnalysisState.CANCELLED)
        db.commit()

        assert analysis.failure_stage is None
        assert progress_percentage(analysis) == 0

    def test_an_unrecognized_failure_stage_string_falls_back_safely(self, db, analysis) -> None:
        # Defensive branch: `failure_stage` is always a real `AnalysisState`
        # value in practice (`transition()` sets it from `from_state.value`),
        # but `progress_percentage` must not crash if that ever isn't true.
        from app.analysis.state_machine import transition

        transition(db, analysis, AnalysisState.FAILED, failure_stage="not_a_real_state")
        db.commit()

        assert progress_percentage(analysis) == progress_percentage_for_state(
            AnalysisState.FAILED
        )
