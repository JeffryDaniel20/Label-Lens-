"""Drives every `GoldenCase` through the REAL pipeline stages
(`app.analysis.stages`) against a throwaway, run-scoped SQLite database -
never the application's own configured database, so `make eval`/CI can run
repeatedly without ever touching real tenant data (see `evals.cli` for
where that throwaway engine is actually created).

Reuses the real stage functions and the real, published `in-fssai-food`
pack rule engine exactly as `app.analysis.worker.run_analysis_stage` does
in production - never a parallel, hand-rolled re-implementation that could
silently drift from what the real pipeline actually does. The only stand-in
is the LLM provider itself (`_StubProvider`, the same pattern every
pipeline-stage test in this codebase already uses): each case's
`extraction_json` plays the role of "what the model would have returned,"
because this session has no live LLM credentials (the same D-03/P3-T5
class of gap) - not because extraction itself is faked here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis import service as analysis_service
from app.analysis.models import Analysis, AnalysisState
from app.analysis.stages import (
    _classifying,
    _evidence_verification,
    _normalizing,
    _rule_eval,
    _scoring,
)
from app.analysis.state_machine import transition
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.extraction.llm.base import ProviderResponse, ProviderUsage
from app.extraction.models import ExtractedField, Extraction
from app.extraction.service import extract_for_analysis
from app.findings.models import Finding
from app.identity.models import Organization
from app.rules.loader import RulePack
from app.rules.publish import publish_pack
from app.vision.models import OcrResult, OcrTokenRow
from evals.dataset import GoldenCase, GoldenDataset

_PRE_RULE_EVAL_STATES = (
    AnalysisState.VALIDATING,
    AnalysisState.PREPROCESSING,
    AnalysisState.OCR,
    AnalysisState.EXTRACTING,
    AnalysisState.EVIDENCE_VERIFICATION,
    AnalysisState.NORMALIZING,
    AnalysisState.CLASSIFYING,
    AnalysisState.RULE_EVAL,
)


class _StubProvider:
    """The same `_StubProvider` shape `tests/integration/test_pipeline_stages.py`
    already uses - `generate()` returns exactly the case's own
    `extraction_json`, standing in for a real LLM call this session has no
    credentials for."""

    name = "eval-stub"

    def __init__(self, text: str) -> None:
        self.text = text

    def generate(
        self, *, system_instruction: str, prompt: str, schema: type, model: str
    ) -> ProviderResponse:
        return ProviderResponse(
            text=self.text, usage=ProviderUsage(tokens_in=1, tokens_out=1), model=model
        )


@dataclass(frozen=True, slots=True)
class CaseResult:
    case_id: str
    image_quality: str
    expected_fields: dict[str, str | None]
    actual_fields: dict[str, str | None]
    verified_flags: list[bool]
    expected_findings: dict[str, str]
    actual_findings: dict[str, str]
    finding_confidences: dict[str, float]
    confidence_tier: str | None


def _bootstrap_analysis(db: Session, case: GoldenCase) -> Analysis:
    org = Organization(name="Eval Org", slug=f"eval-org-{uuid.uuid4().hex}")
    db.add(org)
    db.flush()
    product = Product(
        organization_id=org.id,
        name="Eval Product",
        internal_sku=f"EVAL-{uuid.uuid4().hex[:12]}",
        category_hint=case.category_hint,
        market_codes=list(case.market_codes),
    )
    db.add(product)
    db.flush()
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=1)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key=f"org/{org.id}/pv/{version.id}/render/eval/1.png",
        original_filename="label.png",
        sha256=uuid.uuid4().hex.ljust(64, "0"),
        mime="image/png",
        bytes=1,
        status=FileStatus.READY,
    )
    db.add(file)
    db.flush()
    page = FilePage(
        organization_id=org.id,
        file_id=file.id,
        page_no=1,
        width=40,
        height=30,
        render_key=file.storage_key,
    )
    db.add(page)
    db.flush()

    ocr_result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="eval-stub",
        engine_version="1",
        avg_confidence=0.9,
        raw=[],
    )
    db.add(ocr_result)
    db.flush()
    for token in case.ocr_tokens:
        db.add(
            OcrTokenRow(
                organization_id=org.id,
                file_page_id=page.id,
                ocr_result_id=ocr_result.id,
                text=token.text,
                confidence=0.9,
                x1=token.x1,
                y1=token.y1,
                x2=token.x2,
                y2=token.y2,
                line_no=token.line_no,
            )
        )
    db.flush()

    file_hash = analysis_service.compute_file_set_hash(
        db, organization_id=org.id, version_id=version.id
    )
    analysis, _created = analysis_service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return analysis


def run_case(db: Session, case: GoldenCase) -> CaseResult:
    """Runs one case through the entire real pipeline
    (`extracting -> evidence_verification -> normalizing -> classifying ->
    rule_eval -> scoring`) and reads back exactly what a real reviewer would
    see afterward - `ExtractedField.value_raw`/`verified` and `Finding.status`/
    `confidence` - never a value computed independently of the pipeline."""
    analysis = _bootstrap_analysis(db, case)

    extract_for_analysis(
        db,
        provider=_StubProvider(case.extraction_json),
        organization_id=analysis.organization_id,
        analysis_id=analysis.id,
        product_version_id=analysis.product_version_id,
        model="eval-stub",
        escalation_model="eval-stub",
    )
    db.commit()
    assert _evidence_verification(db, analysis) is None
    db.commit()
    assert _normalizing(db, analysis) is None
    db.commit()
    assert _classifying(db, analysis) is None
    db.commit()

    for state in _PRE_RULE_EVAL_STATES:
        transition(db, analysis, state)
    _rule_eval(db, analysis)
    db.commit()
    transition(db, analysis, AnalysisState.SCORING)
    next_state = _scoring(db, analysis)
    transition(db, analysis, next_state or AnalysisState.COMPLETED)
    db.commit()
    db.refresh(analysis)

    extraction = db.scalar(
        select(Extraction)
        .where(Extraction.analysis_id == analysis.id)
        .order_by(Extraction.created_at.desc())
    )
    assert extraction is not None
    fields = db.scalars(
        select(ExtractedField).where(ExtractedField.extraction_id == extraction.id)
    ).all()
    actual_fields = {f.field_path: f.value_raw for f in fields}
    verified_flags = [bool(f.verified) for f in fields if f.value_raw is not None]

    findings = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
    actual_findings = {f.rule_key: f.status.value for f in findings}
    finding_confidences = {f.rule_key: f.confidence for f in findings}

    return CaseResult(
        case_id=case.case_id,
        image_quality=case.image_quality,
        expected_fields=case.expected_fields,
        actual_fields=actual_fields,
        verified_flags=verified_flags,
        expected_findings=case.expected_findings,
        actual_findings=actual_findings,
        finding_confidences=finding_confidences,
        confidence_tier=analysis.confidence_tier.value if analysis.confidence_tier else None,
    )


def run_dataset(db: Session, dataset: GoldenDataset, ruleset_pack: RulePack) -> list[CaseResult]:
    """Publishes `ruleset_pack` once (idempotent - `publish_pack` returns
    the existing row if this exact content is already published) and runs
    every case in `dataset` against it, in order."""
    publish_pack(db, ruleset_pack)
    db.commit()
    return [run_case(db, case) for case in dataset.cases]
