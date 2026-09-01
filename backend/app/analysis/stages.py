"""Pipeline stage functions and the ordered stage sequence (P5-T2).

Each state between `queued` and `scoring` has a `StageFn` registered against
it in `STAGE_FUNCTIONS`: `advance_analysis()` calls the function registered
for the analysis's *current* state, then transitions it to the next state in
`DEFAULT_NEXT_STATE` - one call, one stage, one durable transition. There is
deliberately no separate "checkpoint" table: the transition commit *is* the
checkpoint. Resuming a crashed or retried job means re-loading the analysis
and calling `advance_analysis()` again - since `analysis.state` already
reflects every stage that completed and committed before the crash, nothing
already done gets redone. If a stage's own function raises before completing,
the transition never happens and the analysis stays exactly where it was -
retrying re-attempts only that one stage, not the ones before it.

The functions registered here for `ocr`/`extracting`/`normalizing`/
`classifying`/`rule_eval` are honest placeholders, not real pipeline logic:

- `ocr` needs a real PaddleOCR call. P3-T2's adapter exists and is proven,
  but only in a second Python 3.12 virtualenv - the interpreter this worker
  would actually run under has no compatible `paddlepaddle` wheel.
- `extracting` needs P3-T5 (LLM extraction), blocked on a missing
  `ANTHROPIC_API_KEY`.
- `normalizing`/`classifying`/`rule_eval` are themselves downstream of
  `extracting`'s output - there are no facts to normalize, classify, or
  evaluate against without a real extraction to work from.

What P5-T2 is responsible for is that the queue correctly drives whatever
function *is* registered for a state, retries it, checkpoints past it, and
resumes correctly after a crash - proven here against instrumented fake
stage functions that record their own call counts. Wiring the real work into
each placeholder is what happens once each one's own blocker clears; no
change to `advance_analysis()` or the worker will be needed to do it.
"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from app.analysis.models import TERMINAL_STATES, Analysis, AnalysisState
from app.analysis.state_machine import transition

type StageFn = Callable[[Session, Analysis], AnalysisState | None]

STAGE_SEQUENCE: tuple[AnalysisState, ...] = (
    AnalysisState.VALIDATING,
    AnalysisState.PREPROCESSING,
    AnalysisState.OCR,
    AnalysisState.EXTRACTING,
    AnalysisState.NORMALIZING,
    AnalysisState.CLASSIFYING,
    AnalysisState.RULE_EVAL,
    AnalysisState.SCORING,
)

# The state a stage transitions to by default once its function returns
# without an explicit override. `scoring`'s real successor depends on the
# analysis's own confidence tier (`needs_review` vs `completed`) - a real
# scoring stage function decides that itself by returning the state
# explicitly; the default here is the "nothing computed a tier yet" case.
DEFAULT_NEXT_STATE: dict[AnalysisState, AnalysisState] = {
    AnalysisState.QUEUED: AnalysisState.VALIDATING,
    AnalysisState.VALIDATING: AnalysisState.PREPROCESSING,
    AnalysisState.PREPROCESSING: AnalysisState.OCR,
    AnalysisState.OCR: AnalysisState.EXTRACTING,
    AnalysisState.EXTRACTING: AnalysisState.NORMALIZING,
    AnalysisState.NORMALIZING: AnalysisState.CLASSIFYING,
    AnalysisState.CLASSIFYING: AnalysisState.RULE_EVAL,
    AnalysisState.RULE_EVAL: AnalysisState.SCORING,
    AnalysisState.SCORING: AnalysisState.COMPLETED,
}

# States where the pipeline stops advancing on its own: a terminal state
# (nothing left to do) or `needs_review`/`review` (waiting on a human -
# P6's review workflow, not this queue, is what moves it from there).
STOPPING_STATES: frozenset[AnalysisState] = TERMINAL_STATES | {
    AnalysisState.NEEDS_REVIEW,
    AnalysisState.REVIEW,
}


def _placeholder(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Does nothing: an honest stand-in until this stage's real prerequisite
    (see the module docstring) is unblocked."""
    return None


STAGE_FUNCTIONS: dict[AnalysisState, StageFn] = {
    AnalysisState.VALIDATING: _placeholder,
    AnalysisState.PREPROCESSING: _placeholder,
    AnalysisState.OCR: _placeholder,
    AnalysisState.EXTRACTING: _placeholder,
    AnalysisState.NORMALIZING: _placeholder,
    AnalysisState.CLASSIFYING: _placeholder,
    AnalysisState.RULE_EVAL: _placeholder,
    AnalysisState.SCORING: _placeholder,
}


def advance_analysis(
    db: Session,
    analysis: Analysis,
    *,
    correlation_id: str | None = None,
    worker_id: str | None = None,
) -> AnalysisState:
    """Do the analysis's *current* state's stage work (if any is
    registered) and transition to the next state. A no-op, returning the
    unchanged state, if the analysis has already stopped (terminal or
    awaiting human review) - the safe response to a duplicate/late job
    delivery after the analysis has already moved on.

    Raises whatever the stage function raises, *before* any transition -
    the caller (the worker task) owns committing or rolling back; a raised
    exception here always leaves the analysis in its pre-call state.
    """
    if analysis.state in STOPPING_STATES:
        return analysis.state

    stage_fn = STAGE_FUNCTIONS.get(analysis.state)
    override = stage_fn(db, analysis) if stage_fn is not None else None
    next_state = override or DEFAULT_NEXT_STATE.get(analysis.state)
    if next_state is None:
        return analysis.state

    transition(db, analysis, next_state, correlation_id=correlation_id, worker_id=worker_id)
    return analysis.state
