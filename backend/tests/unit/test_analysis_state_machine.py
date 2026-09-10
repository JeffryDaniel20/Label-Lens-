"""Unit tests for the analysis state machine (P5-T1).

No live services needed: `transition()` only needs a SQLAlchemy session
(the shared `db` fixture, SQLite-backed) to flush an `Analysis` row and its
`AnalysisEvent`s - nothing here depends on PostgreSQL specifically.
"""

from __future__ import annotations

import pytest

from app.analysis.models import TERMINAL_STATES, Analysis, AnalysisEvent, AnalysisState
from app.analysis.state_machine import ALLOWED_TRANSITIONS, _fit_reason, transition
from app.catalog.models import Product, ProductVersion
from app.platform.errors import StateInvalid
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
    row = Analysis(
        organization_id=org.id,
        product_version_id=version.id,
        state=AnalysisState.QUEUED,
        idempotency_key="x" * 64,
    )
    db.add(row)
    db.flush()
    return row


ALL_STATES = list(AnalysisState)


class TestLegalTransitions:
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (AnalysisState.QUEUED, AnalysisState.VALIDATING),
            (AnalysisState.VALIDATING, AnalysisState.PREPROCESSING),
            (AnalysisState.PREPROCESSING, AnalysisState.OCR),
            (AnalysisState.OCR, AnalysisState.EXTRACTING),
            (AnalysisState.EXTRACTING, AnalysisState.EVIDENCE_VERIFICATION),
            (AnalysisState.EVIDENCE_VERIFICATION, AnalysisState.NORMALIZING),
            (AnalysisState.NORMALIZING, AnalysisState.CLASSIFYING),
            (AnalysisState.CLASSIFYING, AnalysisState.RULE_EVAL),
            (AnalysisState.RULE_EVAL, AnalysisState.SCORING),
            (AnalysisState.SCORING, AnalysisState.NEEDS_REVIEW),
            (AnalysisState.SCORING, AnalysisState.COMPLETED),
            (AnalysisState.NEEDS_REVIEW, AnalysisState.REVIEW),
            (AnalysisState.REVIEW, AnalysisState.COMPLETED),
        ],
    )
    def test_the_documented_happy_path_transitions_succeed(
        self, db, analysis, from_state: AnalysisState, to_state: AnalysisState
    ) -> None:
        analysis.state = from_state
        db.flush()
        result = transition(db, analysis, to_state)
        assert result.state is to_state

    @pytest.mark.parametrize(
        "from_state",
        [s for s in ALL_STATES if s not in TERMINAL_STATES],
    )
    def test_any_non_terminal_state_can_fail(self, db, analysis, from_state: AnalysisState) -> None:
        analysis.state = from_state
        db.flush()
        result = transition(db, analysis, AnalysisState.FAILED)
        assert result.state is AnalysisState.FAILED

    @pytest.mark.parametrize(
        "from_state",
        [s for s in ALL_STATES if s not in TERMINAL_STATES],
    )
    def test_any_non_terminal_state_can_be_cancelled(
        self, db, analysis, from_state: AnalysisState
    ) -> None:
        analysis.state = from_state
        db.flush()
        result = transition(db, analysis, AnalysisState.CANCELLED)
        assert result.state is AnalysisState.CANCELLED


class TestIllegalTransitionsAreRejected:
    @pytest.mark.parametrize(
        ("from_state", "to_state"),
        [
            (AnalysisState.QUEUED, AnalysisState.OCR),  # skipping stages
            (AnalysisState.QUEUED, AnalysisState.COMPLETED),  # skipping the whole pipeline
            (AnalysisState.VALIDATING, AnalysisState.QUEUED),  # going backwards
            # P3-T6: evidence verification is its own mandatory checkpoint -
            # extraction can no longer transition straight to normalizing,
            # skipping the citation check entirely.
            (AnalysisState.EXTRACTING, AnalysisState.NORMALIZING),
            (AnalysisState.SCORING, AnalysisState.REVIEW),  # must go through needs_review
            (AnalysisState.NEEDS_REVIEW, AnalysisState.COMPLETED),  # must go through review
            (AnalysisState.COMPLETED, AnalysisState.QUEUED),  # terminal -> anything
            (AnalysisState.FAILED, AnalysisState.QUEUED),
            (AnalysisState.CANCELLED, AnalysisState.VALIDATING),
        ],
    )
    def test_illegal_transitions_raise_state_invalid(
        self, db, analysis, from_state: AnalysisState, to_state: AnalysisState
    ) -> None:
        analysis.state = from_state
        db.flush()
        with pytest.raises(StateInvalid):
            transition(db, analysis, to_state)

    @pytest.mark.parametrize("terminal_state", sorted(TERMINAL_STATES))
    def test_no_transition_at_all_is_legal_from_a_terminal_state(
        self, db, analysis, terminal_state: AnalysisState
    ) -> None:
        analysis.state = terminal_state
        db.flush()
        assert ALLOWED_TRANSITIONS[terminal_state] == frozenset()
        for candidate in ALL_STATES:
            with pytest.raises(StateInvalid):
                transition(db, analysis, candidate)

    def test_a_rejected_transition_does_not_change_the_analysis_state(
        self, db, analysis
    ) -> None:
        analysis.state = AnalysisState.QUEUED
        db.flush()
        with pytest.raises(StateInvalid):
            transition(db, analysis, AnalysisState.COMPLETED)
        assert analysis.state is AnalysisState.QUEUED


class TestTerminalSideEffects:
    def test_finished_at_is_set_on_reaching_any_terminal_state(self, db, analysis) -> None:
        assert analysis.finished_at is None
        transition(db, analysis, AnalysisState.VALIDATING)
        assert analysis.finished_at is None
        transition(db, analysis, AnalysisState.CANCELLED)
        assert analysis.finished_at is not None

    def test_failing_records_the_failure_stage_and_retryable_flag(self, db, analysis) -> None:
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.FAILED, failure_stage="validating", retryable=True)
        assert analysis.failure_stage == "validating"
        assert analysis.retryable is True

    def test_failure_stage_defaults_to_the_state_it_failed_from(self, db, analysis) -> None:
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.FAILED)
        assert analysis.failure_stage == "validating"
        assert analysis.retryable is False


class TestReasonIsFitToItsColumn:
    """Found live: a real Gemini `429`/`503` error body can run well past
    `AnalysisEvent.reason`'s `String(500)` column, which made the failure-
    handling transition itself raise `StringDataRightTruncation` - the
    analysis never even reached `failed`, it just stayed stuck retrying
    forever. `transition()` must never let an oversized `reason` reach the
    `INSERT` in the first place."""

    def test_a_reason_within_the_limit_is_stored_unchanged(self, db, analysis) -> None:
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.FAILED, reason="short and simple")
        event = db.query(AnalysisEvent).filter_by(to_state=AnalysisState.FAILED).one()
        assert event.reason == "short and simple"

    def test_an_oversized_reason_is_truncated_to_fit(self, db, analysis) -> None:
        long_reason = "x" * 800
        transition(db, analysis, AnalysisState.VALIDATING)
        transition(db, analysis, AnalysisState.FAILED, reason=long_reason)
        event = db.query(AnalysisEvent).filter_by(to_state=AnalysisState.FAILED).one()
        assert event.reason is not None
        assert len(event.reason) == 500
        assert event.reason.endswith("…")

    def test_fit_reason_leaves_none_and_short_strings_alone(self) -> None:
        assert _fit_reason(None) is None
        assert _fit_reason("fine") == "fine"
        assert _fit_reason("x" * 500) == "x" * 500

    def test_fit_reason_truncates_anything_longer(self) -> None:
        result = _fit_reason("x" * 501)
        assert result is not None
        assert len(result) == 500
        assert result.endswith("…")


class TestEveryStateIsReachableAndCoversAllTransitions:
    def test_every_analysisstate_member_appears_in_the_transition_table(self) -> None:
        assert set(ALLOWED_TRANSITIONS.keys()) == set(AnalysisState)

    def test_every_terminal_state_has_no_outgoing_transitions(self) -> None:
        for state in TERMINAL_STATES:
            assert ALLOWED_TRANSITIONS[state] == frozenset()

    def test_every_non_terminal_state_can_reach_failed_and_cancelled(self) -> None:
        for state, targets in ALLOWED_TRANSITIONS.items():
            if state in TERMINAL_STATES:
                continue
            assert AnalysisState.FAILED in targets
            assert AnalysisState.CANCELLED in targets
