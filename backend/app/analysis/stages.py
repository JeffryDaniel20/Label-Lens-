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

`validating`, `ocr`, `extracting`, `normalizing`, and `classifying` are all
real now - the full vertical slice from a rasterized page to a classified,
normalized fact set. `preprocessing` stays a placeholder deliberately, not
because it is blocked: `app.vision.ocr.service.run_ocr` already calls
`preprocess()` itself for each page immediately before OCR runs on it (P3-T1
composed with P3-T2, not a gap), so a separate top-level preprocessing pass
would either duplicate that work or run it on pages `ocr` hasn't reached yet
for no benefit. Only `rule_eval` and `scoring` remain honest placeholders
for a real blocker:

- `rule_eval` needs a real published ruleset to evaluate against, which
  waits on **D-01** (first jurisdiction) actually being decided, not just
  proposed - `app/rules/evaluator.py` itself is done and 100%-tested.
- `scoring` is downstream of `rule_eval`'s findings (confidence tiering
  needs findings to weigh); nothing to score without them yet.

Without `rule_eval` producing anything, `scoring`'s default successor
(`DEFAULT_NEXT_STATE[SCORING] = COMPLETED`) means a real analysis today
walks all the way from `queued` to `completed` on its own - with genuine
OCR tokens, a genuine LLM extraction, genuinely normalized/classified
facts, and zero compliance findings, because there is nothing yet to
evaluate them against. That absence is itself honest: an analysis that
reaches `completed` with no findings is not "compliant," it is "nothing
was checked" - `rule_eval`'s eventual wiring is what makes a `completed`
analysis mean something.

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


def _validating(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Confirms rasterized pages actually exist for this product version's
    ready files before the pipeline spends any real work on it. Ingestion
    (P2-T3/P2-T4) already rasterizes every ready file at upload time, so a
    version with no pages here means something upstream is broken, not that
    retrying might help - a permanent failure, and the same check `_ocr`
    would otherwise hit anyway, just earlier and more cheaply."""
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.catalog.models import File, FilePage, FileStatus

    has_pages = db.scalar(
        select(FilePage.id)
        .join(File, File.id == FilePage.file_id)
        .where(
            File.organization_id == analysis.organization_id,
            File.product_version_id == analysis.product_version_id,
            File.status == FileStatus.READY,
        )
        .limit(1)
    )
    if has_pages is None:
        raise PermanentStageError(
            "No rasterized pages exist for this product version's ready files."
        )
    return None


def _ocr(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Runs OCR over every rasterized page of this product version's ready
    files, via `app.vision.ocr.service.run_ocr` (P3-T2) - already proven
    against a real PaddleOCR engine (see IMPLEMENTATION.md's P3-T2
    evidence), just never called from the pipeline until now.

    A page that already has an `OcrResult` is skipped - duplicate job
    delivery (a job redelivered after it actually succeeded) must not re-run
    OCR on work already done. That said, this stage is still one atomic unit
    like every other in this module: nothing here commits until the whole
    function returns, so a crash partway through re-OCRs every page from
    *this* attempt, not just the one that failed. Per-page checkpointing
    within a single stage is a real possible refinement, not attempted here.
    """
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError, TransientStageError
    from app.catalog.models import File, FilePage, FileStatus
    from app.platform.config import get_settings
    from app.storage.client import build_storage_client
    from app.vision.models import OcrResult
    from app.vision.ocr.service import get_default_ocr_engine, run_ocr

    pages = list(
        db.scalars(
            select(FilePage)
            .join(File, File.id == FilePage.file_id)
            .where(
                File.organization_id == analysis.organization_id,
                File.product_version_id == analysis.product_version_id,
                File.status == FileStatus.READY,
            )
            .order_by(File.id, FilePage.page_no)
        ).all()
    )
    if not pages:
        raise PermanentStageError("No rasterized pages exist for this product version.")

    already_done = {
        row.file_page_id
        for row in db.scalars(
            select(OcrResult).where(
                OcrResult.organization_id == analysis.organization_id,
                OcrResult.file_page_id.in_([p.id for p in pages]),
            )
        ).all()
    }

    try:
        engine = get_default_ocr_engine()
    except Exception as exc:
        # Missing/broken in *this* environment is permanent from this
        # worker's perspective - every worker runs the same image, so
        # retrying elsewhere cannot succeed either.
        raise PermanentStageError(f"OCR engine unavailable: {exc}") from exc

    storage = build_storage_client(get_settings())

    for page in pages:
        if page.id in already_done:
            continue
        try:
            run_ocr(
                db, storage, engine, organization_id=analysis.organization_id, file_page=page
            )
        except ValueError as exc:
            # `run_ocr` raises `ValueError` when the rendered page can't be
            # decoded as an image at all - a corrupt render, not something a
            # retry fixes.
            raise PermanentStageError(f"Page {page.id} could not be decoded: {exc}") from exc
        except Exception as exc:
            raise TransientStageError(f"OCR failed on page {page.id}: {exc}") from exc

    return None


def _normalizing(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Deterministic Python normalization (P3-T7) over what `_extracting`
    persisted - IMPLEMENTATION.md §8 step 7's whole point: units, locale
    numbers, and dates are parsed here, never left to the model.

    Two different things get written, matching P3-T5's own division
    (`app/extraction/service.py`'s module docstring): the ingredient list is
    *structural* - P3-T4's schema defines `ingredients.items` as a real
    parsed list, not text - so it is written back into
    `Extraction.payload`/`LabelFacts` itself; everything else normalized
    here (dates, quantity) is *auxiliary* to an as-printed value that stays
    in `LabelFacts` unchanged, so it lands on `ExtractedField.value_norm`/
    `unit` instead - the columns P3-T5 added for exactly this, needing no
    second migration.

    A field that fails to normalize is left un-normalized rather than
    failing the whole stage: deterministic parsing over data an LLM already
    read is not something a retry fixes, and one unparseable date should not
    block every other field a reviewer could still use.
    """
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.extraction import facts as facts_schema
    from app.extraction.models import ExtractedField, Extraction
    from app.extraction.normalize.dates import normalize_label_date
    from app.extraction.normalize.ingredients import parse_ingredients
    from app.extraction.normalize.units import parse_quantity

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise PermanentStageError("No extraction exists for this analysis to normalize.")

    label_facts = facts_schema.LabelFacts.model_validate(extraction.payload)
    declared_text = label_facts.ingredients.declared_text.value
    if declared_text:
        items = parse_ingredients(declared_text)
        if items:
            label_facts = label_facts.model_copy(
                update={
                    "ingredients": label_facts.ingredients.model_copy(
                        update={"items": facts_schema.Fact.found(items)}
                    )
                }
            )
    extraction.payload = label_facts.model_dump(mode="json")

    field_rows = {
        row.field_path: row
        for row in db.scalars(
            select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
        ).all()
    }

    for path in ("dates.manufacture_date", "dates.expiry_or_best_before"):
        row = field_rows.get(path)
        if row and row.value_raw:
            try:
                row.value_norm = {"iso": normalize_label_date(row.value_raw)}
            except ValueError:
                pass  # left un-normalized; the raw text is still there

    quantity_row = field_rows.get("quantity.net_quantity")
    if quantity_row and quantity_row.value_raw:
        try:
            quantity = parse_quantity(quantity_row.value_raw)
        except ValueError:
            pass
        else:
            quantity_row.value_norm = {"value": quantity.value}
            quantity_row.unit = quantity.unit

    db.flush()
    return None


def _classifying(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Category + jurisdiction routing (P3-T9), now wired against the real
    (normalized) fact set instead of hand-written fixtures.

    An abstention (`classify()` returning `abstained=True`) is not a stage
    failure - it is the correct, honest outcome for a label that doesn't
    confidently say what it is, and leaves `Analysis.category`/
    `jurisdictions` unset rather than a guessed routing that `rule_eval`
    would silently trust later. `ClassificationResult`'s own fields already
    encode this (`category=None`, `jurisdictions=()`), so no special-casing
    is needed here - the assignment below is honest either way.
    """
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.catalog.models import Product, ProductVersion
    from app.classification.classifier import UserHints, classify
    from app.extraction import facts as facts_schema
    from app.extraction.models import Extraction

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise PermanentStageError("No extraction exists for this analysis to classify.")
    label_facts = facts_schema.LabelFacts.model_validate(extraction.payload)

    version = db.get(ProductVersion, analysis.product_version_id)
    product = db.get(Product, version.product_id) if version is not None else None
    hints = UserHints(
        category_hint=product.category_hint if product else None,
        market_codes=tuple(product.market_codes or ()) if product else (),
    )

    result = classify(label_facts, hints)
    analysis.category = result.category
    analysis.jurisdictions = list(result.jurisdictions)
    analysis.category_confidence = result.category_confidence
    analysis.jurisdiction_confidence = result.jurisdiction_confidence
    db.flush()
    return None


STAGE_FUNCTIONS: dict[AnalysisState, StageFn] = {
    AnalysisState.VALIDATING: _validating,
    AnalysisState.PREPROCESSING: _placeholder,
    AnalysisState.OCR: _ocr,
    AnalysisState.EXTRACTING: _extracting,
    AnalysisState.NORMALIZING: _normalizing,
    AnalysisState.CLASSIFYING: _classifying,
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
