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

`extracting` is real (P3-T5): it calls the configured LLM provider through
`app/extraction/`. The rest are still honest placeholders, not real pipeline
logic:

- `ocr` needs a real PaddleOCR call. P3-T2's adapter exists and is proven,
  but only in a second Python 3.12 virtualenv - the interpreter this worker
  would actually run under has no compatible `paddlepaddle` wheel. (The
  deployed image *is* `python:3.12-slim`, so this is a local-environment
  gap rather than a production one.)
- `normalizing`/`classifying`/`rule_eval` are downstream of `extracting`'s
  output. Their libraries all exist and are tested (P3-T7, P3-T9, P4-T3);
  what they still need is wiring, plus - for `rule_eval` - a real published
  ruleset, which waits on D-01.

Note on cost: `_extracting` records the provider's reported **token** usage
via P5-T5's `record_stage_cost`, but not cents - converting tokens to money
needs a per-model price table that changes independently of this codebase.
`total_cost_cents` therefore stays 0 until that table exists; tokens are
recorded truthfully rather than money being estimated.

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


def _extracting(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Real LLM extraction (P3-T5), reached through the provider-neutral
    adapter selected by `LABELLENS_LLM_PROVIDER` (D-06).

    Failure classification is deliberate, and uses P5-T3's vocabulary:

    - a missing provider/credential, or an extraction that never satisfied
      the schema, is `PermanentStageError` - retrying the same input against
      the same configuration cannot succeed, so it dead-letters immediately
      rather than burning three attempts' worth of tokens;
    - a provider transport failure (rate limit, timeout, 5xx) is
      `TransientStageError` and retries with backoff.

    Provider spend is recorded on the analysis (P5-T5) *before* the
    transition, so a run's cost is durable even for an extraction that later
    fails a downstream stage.
    """
    from app.analysis.costs import record_stage_cost  # noqa: PLC0415 - avoids an import cycle
    from app.analysis.retry_policy import PermanentStageError, TransientStageError
    from app.extraction import llm as llm_module
    from app.extraction import service as extraction_service
    from app.platform.config import get_settings

    settings = get_settings()
    try:
        provider = llm_module.build_provider(settings)
    except llm_module.ProviderNotConfigured as exc:
        raise PermanentStageError(str(exc)) from exc

    try:
        outcome = extraction_service.extract_for_analysis(
            db,
            provider=provider,
            organization_id=analysis.organization_id,
            analysis_id=analysis.id,
            product_version_id=analysis.product_version_id,
            model=settings.llm_model,
            escalation_model=settings.llm_escalation_model,
        )
    except extraction_service.ExtractionFailed as exc:
        raise PermanentStageError(str(exc)) from exc
    except llm_module.ProviderError as exc:
        raise TransientStageError(str(exc)) from exc

    record_stage_cost(
        db,
        analysis,
        stage=AnalysisState.EXTRACTING.value,
        provider=provider.name,
        tokens_in=outcome.tokens_in,
        tokens_out=outcome.tokens_out,
    )
    return None


STAGE_FUNCTIONS: dict[AnalysisState, StageFn] = {
    AnalysisState.VALIDATING: _placeholder,
    AnalysisState.PREPROCESSING: _placeholder,
    AnalysisState.OCR: _placeholder,
    AnalysisState.EXTRACTING: _extracting,
    AnalysisState.NORMALIZING: _placeholder,
    AnalysisState.CLASSIFYING: _placeholder,
    AnalysisState.RULE_EVAL: _placeholder,
    AnalysisState.SCORING: _placeholder,
}


# Progress reporting (P5-T5): `queued` through `completed` in pipeline
# order, each step worth an equal share - crude but honest given the real
# per-stage bodies (OCR, extraction) don't exist yet to weight by actual
# duration.
_PROGRESS_SEQUENCE: tuple[AnalysisState, ...] = (
    AnalysisState.QUEUED,
    *STAGE_SEQUENCE,
    AnalysisState.COMPLETED,
)


def progress_percentage_for_state(state: AnalysisState) -> int:
    """0-100 for a single state, with no notion of *how* an analysis got
    there - `needs_review`/`review` count as `scoring` (the automated
    pipeline's own work is done; what's left is a human, not another
    stage). A bare `failed`/`cancelled` (no more specific stage known)
    reports 0 rather than guessing."""
    if state in (AnalysisState.NEEDS_REVIEW, AnalysisState.REVIEW):
        state = AnalysisState.SCORING
    try:
        index = _PROGRESS_SEQUENCE.index(state)
    except ValueError:
        return 0
    return round(index / (len(_PROGRESS_SEQUENCE) - 1) * 100)


def progress_percentage(analysis: Analysis) -> int:
    """0-100 for a whole `Analysis`. `failed` reports how far the pipeline
    actually got (`failure_stage`, always set by `transition()` for a
    `failed` outcome) rather than 100%, which would misleadingly read as
    success. `cancelled` never carries a `failure_stage` (that field is
    `failed`-specific, see `state_machine.transition`) and reports 0%
    rather than guessing how far a manually-cancelled run had gotten."""
    state = analysis.state
    if state in (AnalysisState.FAILED, AnalysisState.CANCELLED) and analysis.failure_stage:
        try:
            state = AnalysisState(analysis.failure_stage)
        except ValueError:
            pass
    return progress_percentage_for_state(state)


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
