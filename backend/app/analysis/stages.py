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

`validating`, `ocr`, `extracting`, `verifying_evidence`, `normalizing`, and
`classifying` are all real now - the full vertical slice from a rasterized
page to a classified, normalized fact set. `verifying_evidence` (P3-T6) is
its own stage between `extracting` and `normalizing`, not folded into
`extracting` itself: `app.extraction.evidence.verify_extraction` already did
the real, deterministic, LLM-free citation check (fuzzy-matching every cited
value against the real `OcrTokenRow` rows it claims to come from, demoting
anything that doesn't hold up before `rule_eval` could ever see it), but it
used to run as a side effect inside `extract_for_analysis` - meaning a crash
during verification meant redoing the LLM call too. Splitting it into its
own stage/checkpoint means a retry here only re-runs this cheap check, never
re-burns tokens on an extraction that already committed successfully.
`preprocessing` stays a placeholder deliberately, not
because it is blocked: `app.vision.ocr.service.run_ocr` already calls
`preprocess()` itself for each page immediately before OCR runs on it (P3-T1
composed with P3-T2, not a gap), so a separate top-level preprocessing pass
would either duplicate that work or run it on pages `ocr` hasn't reached yet
for no benefit.

`rule_eval` is real now too (P5-T4, 2026-09-10): `_rule_eval` resolves
whether a published `Ruleset` actually exists for the analysis's own
classified jurisdiction/category (`app.rules.publish.find_active_ruleset`)
and only evaluates + persists findings if one does. D-01 (first
jurisdiction) is resolved - India/FSSAI, packaged food - and a real pack is
published (`in-fssai-food` v1.0.0, P4-T5), so for that one jurisdiction/
category this stage now genuinely evaluates real regulation content and
persists real findings; for every other jurisdiction/category
`find_active_ruleset` still returns `None` and this stage advances having
done nothing, exactly like the placeholder it originally replaced - both
outcomes are the same honest, no-fabrication design, not two different code
paths. An abstained classification (`analysis.category is None`, P3-T9) is
skipped the same honest way - there is nothing to look a ruleset up for.

`scoring` is real now too (P3-T8): `app.confidence.tiers.compute_analysis_tier`
rolls every extracted field's confidence *and* the analysis's own
classification (P3-T9) confidence up into one `ConfidenceTier` - an
abstained or weakly-resolved classification forces the tier down exactly
like a missing/demoted field does, so `_classifying` running earlier in
this same chain isn't just informational. `_scoring` routes explicitly -
`high` falls through to `DEFAULT_NEXT_STATE[SCORING] = COMPLETED`, anything
else returns `needs_review` directly, matching §14's "any analysis in
Medium/Low tier ... routes to review." **2026-09-10: it now weighs real
compliance findings whenever `rule_eval` produced any** -
`compute_analysis_tier` narrows to exactly "fields any triggered rule
depends on" (via each persisted `Finding`'s own `evidence_fields`) rather
than every extracted field, so a field no FSSAI rule cares about (e.g.
`claims.items`) no longer forces mandatory review on its own; when no
findings exist at all (an abstained classification, or a jurisdiction/
category D-01 hasn't resolved), it still falls back to the original,
documented-safe superset of every extracted field - see
`app.confidence.tiers`'s own docstring for the full reasoning either way.

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

from app.analysis.models import TERMINAL_STATES, Analysis, AnalysisState, ConfidenceTier
from app.analysis.state_machine import transition

type StageFn = Callable[[Session, Analysis], AnalysisState | None]

STAGE_SEQUENCE: tuple[AnalysisState, ...] = (
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

# The state a stage transitions to by default once its function returns
# without an explicit override. `scoring`'s real successor depends on the
# analysis's own confidence tier (`needs_review` vs `completed`) - `_scoring`
# decides that itself by returning the state explicitly; the default here
# only applies to the `high`-tier case, where it returns `None`.
DEFAULT_NEXT_STATE: dict[AnalysisState, AnalysisState] = {
    AnalysisState.QUEUED: AnalysisState.VALIDATING,
    AnalysisState.VALIDATING: AnalysisState.PREPROCESSING,
    AnalysisState.PREPROCESSING: AnalysisState.OCR,
    AnalysisState.OCR: AnalysisState.EXTRACTING,
    AnalysisState.EXTRACTING: AnalysisState.EVIDENCE_VERIFICATION,
    AnalysisState.EVIDENCE_VERIFICATION: AnalysisState.NORMALIZING,
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


def _evidence_verification(db: Session, analysis: Analysis) -> AnalysisState | None:
    """P3-T6: deterministically checks every extracted field with cited OCR
    tokens against that cited evidence, demoting anything that doesn't check
    out - its own orchestrator stage/checkpoint, not an inline side effect
    of `extracting`.

    Evidence-first, by construction: this stage never calls the LLM or asks
    it anything - it only compares what `_extracting` already persisted
    (values + citations) against `OcrTokenRow` rows that exist independently
    of extraction, so the model that made the claim never gets a vote on
    whether its own citation holds up. A missing, forged (out-of-range, so
    already dropped before this stage ever sees it), or textually
    unsupported citation is never silently accepted: `verify_extraction`
    rewrites the corresponding `LabelFacts` entry to an explicit
    `Fact.missing(...)` *before* `Extraction.payload` is overwritten here, so
    there is no code path by which a later stage could read a value that
    failed this check.

    Deterministic and side-effect-free beyond this analysis's own rows, so
    there is no transient failure mode here worth retrying - the only way
    this stage fails is a genuinely missing prerequisite (`extracting`
    somehow never persisted an `Extraction` row), which a retry cannot fix
    either.
    """
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.extraction import evidence as evidence_gate
    from app.extraction import facts as facts_schema
    from app.extraction.models import Extraction

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise PermanentStageError("No extraction exists for this analysis to verify.")

    label_facts = facts_schema.LabelFacts.model_validate(extraction.payload)
    label_facts, _summary = evidence_gate.verify_extraction(
        db, extraction=extraction, label_facts=label_facts
    )
    extraction.payload = label_facts.model_dump(mode="json")
    db.flush()
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
    from app.vision.ocr import build_ocr_fallback_engine
    from app.vision.ocr.escalation import EscalationPolicy, run_ocr_with_escalation
    from app.vision.ocr.service import get_default_ocr_engine

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

    settings = get_settings()
    storage = build_storage_client(settings)
    fallback_engine = build_ocr_fallback_engine(settings)
    policy = EscalationPolicy(
        confidence_threshold=settings.ocr_fallback_confidence_threshold,
        daily_budget_per_org=settings.ocr_fallback_daily_budget_per_org,
    )

    for page in pages:
        if page.id in already_done:
            continue
        try:
            run_ocr_with_escalation(
                db,
                storage,
                organization_id=analysis.organization_id,
                file_page=page,
                primary_engine=engine,
                fallback_engine=fallback_engine,
                policy=policy,
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


def _rule_eval(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Real rule evaluation (P5-T4) - see the module docstring for why this
    is genuinely still a no-op in this repository today, not a hollow
    wrapper: `find_active_ruleset` always returns `None` until a real
    ruleset is published for some jurisdiction, which needs D-01 decided
    and real rule content (P4-T5), neither of which this stage touches or
    presumes.

    Deterministic and side-effect-free beyond this one analysis's own rows
    (same reasoning as `_evidence_verification`): no transient failure mode
    exists here worth retrying, only a genuinely missing prerequisite.

    **Reproducibility guard, the reason this checks for its own prior work
    first** (the same idempotent-retry precedent `_ocr` already established
    for a page that already has an `OcrResult`): `Finding` rows are
    append-only (migration 0012 - no code path can update or delete one),
    so if this stage ever committed real findings and then crashed before
    its own state-machine transition committed, a naive retry calling
    `persist_findings` a second time would create genuine duplicates that
    nothing could ever clean up. Finding at least one already-persisted
    `Finding` for this analysis means this stage's real work already
    happened; it advances having done nothing *this* time, which is the
    correct, reproducible outcome - the same findings, not doubled ones.
    """
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.db.base import utcnow
    from app.extraction import facts as facts_schema
    from app.extraction.models import Extraction
    from app.extraction.normalize.allergens import ALLERGEN_SYNONYMS
    from app.findings.models import Finding
    from app.findings.service import persist_findings
    from app.rules import evaluator as rules_evaluator
    from app.rules.publish import find_active_ruleset, load_ruleset

    if analysis.category is None or not analysis.jurisdictions:
        # An honest abstention (P3-T9) has nothing to look a ruleset up
        # for - not an error, the same "no analysis proceeds to rules with
        # a guessed category" acceptance criterion that abstention itself
        # exists to satisfy.
        return None

    already_evaluated = db.scalar(
        select(Finding.id).where(Finding.analysis_id == analysis.id).limit(1)
    )
    if already_evaluated is not None:
        return None  # a retried job after a crash - real work already committed

    # MVP scope (see app.classification.classifier's own docstring: "one
    # jurisdiction + one category"): the first classified jurisdiction is
    # the one a ruleset is resolved against.
    jurisdiction = analysis.jurisdictions[0]
    as_of = utcnow().date()
    ruleset = find_active_ruleset(
        db, jurisdiction=jurisdiction, category=analysis.category, as_of=as_of
    )
    if ruleset is None:
        return None  # nothing published yet - honest, not fabricated

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise PermanentStageError("No extraction exists for this analysis to evaluate.")

    _ruleset, rules = load_ruleset(db, ruleset.id)
    label_facts = facts_schema.LabelFacts.model_validate(extraction.payload)
    findings = rules_evaluator.evaluate(
        facts=label_facts.model_dump(mode="json"),
        rules=rules,
        as_of=as_of,
        jurisdiction=jurisdiction,
        category=analysis.category,
        # `in_allergen_dictionary` (used by e.g. `IN-FSSAI-FOOD-ALLERGEN-
        # NAMES-RECOGNIZED`, P4-T5) needs the one real, single-source-of-
        # truth allergen dictionary passed in explicitly - see
        # `app.rules.predicates`'s own docstring for why it is not imported
        # inside the predicate itself.
        predicate_kwargs={"in_allergen_dictionary": {"dictionary": ALLERGEN_SYNONYMS}},
    )
    persist_findings(
        db, analysis=analysis, extraction=extraction, ruleset_id=ruleset.id, findings=findings
    )
    # Pinning (see `app.rules.publish`'s own docstring): this analysis is
    # now permanently tied to the exact ruleset content it was judged
    # against, reproducible byte-for-byte even once newer versions publish.
    analysis.ruleset_version_id = ruleset.id
    db.flush()
    return None


def _scoring(db: Session, analysis: Analysis) -> AnalysisState | None:
    """Confidence tiering (P3-T8), now wired for real. Explicitly returns
    `needs_review` for anything below `high` rather than relying on
    `DEFAULT_NEXT_STATE`, since that default only covers the "nothing
    computed a tier yet" case - see the module docstring."""
    from sqlalchemy import select

    from app.analysis.retry_policy import PermanentStageError
    from app.confidence.tiers import compute_analysis_tier
    from app.extraction.models import Extraction

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    if extraction is None:
        raise PermanentStageError("No extraction exists for this analysis to score.")

    result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)
    analysis.confidence_tier = result.tier
    db.flush()

    if result.tier == ConfidenceTier.HIGH:
        return None
    return AnalysisState.NEEDS_REVIEW


STAGE_FUNCTIONS: dict[AnalysisState, StageFn] = {
    AnalysisState.VALIDATING: _validating,
    AnalysisState.PREPROCESSING: _placeholder,
    AnalysisState.OCR: _ocr,
    AnalysisState.EXTRACTING: _extracting,
    AnalysisState.EVIDENCE_VERIFICATION: _evidence_verification,
    AnalysisState.NORMALIZING: _normalizing,
    AnalysisState.CLASSIFYING: _classifying,
    AnalysisState.RULE_EVAL: _rule_eval,
    AnalysisState.SCORING: _scoring,
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
