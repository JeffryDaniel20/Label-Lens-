"""P6-T5's service layer: deciding on a finding, and correcting an extracted
field. Exercised against a real analysis run through the entire real
pipeline (`_evidence_verification -> _normalizing -> _classifying ->
_rule_eval -> _scoring`) against the real, published `in-fssai-food` pack -
not a hand-built `Finding`/`ExtractedField` fixture - so a "reviewer decides
on a finding" or "reviewer corrects a field" test is exercising exactly what
a real reviewer would see, the same posture `test_fssai_pack_pipeline.py`
already established for `rule_eval` itself.
"""

# ruff: noqa: F811 - each `basic` test parameter below is pytest fixture
# injection by name, not a redefinition of the `basic` fixture imported at
# module scope for pytest to discover it in this file.

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis.models import AnalysisState
from app.analysis.stages import (
    _classifying,
    _evidence_verification,
    _normalizing,
    _rule_eval,
    _scoring,
)
from app.analysis.state_machine import transition
from app.audit.models import ActorType
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding
from app.identity.models import Role
from app.platform.errors import Conflict, NotFound, ValidationFailed
from app.review import service
from app.review.models import DecisionAction, FieldCorrection
from app.rules.loader import load_pack_from_directory
from app.rules.publish import publish_pack
from tests.conftest import make_org, make_user
from tests.integration.test_fssai_pack_pipeline import (
    COMPLIANT_LABEL_JSON,
    PACK_DIR,
    _add_full_ocr_tokens,
    _run_to_rule_eval,
)
from tests.integration.test_pipeline_stages import (
    _extraction_for,
    basic,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.integration


# The real prefix `transition()` requires before `rule_eval`/`scoring` - the
# stage *functions* for these do real work when called directly (as
# `_run_to_rule_eval` already does), but only `advance_analysis` itself ever
# calls `transition()`, so a test driving stages by hand (bypassing the real
# `extracting` stage's live LLM call, as every fixture in this codebase
# does) must also walk `Analysis.state` through the same legal sequence
# `state_machine.ALLOWED_TRANSITIONS` defines, or a state-gated function
# like `create_field_correction` correctly refuses it as still `queued`.
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


def _compliant_analysis(db, basic_rig):
    """A real analysis, fully pipelined against the real FSSAI pack, sitting
    in whatever real stopping state that produces - `completed` given every
    field in `COMPLIANT_LABEL_JSON` is clean and confidently extracted."""
    org, product, version, file, page, analysis = basic_rig
    product.category_hint = "packaged_food"
    product.market_codes = ["IN"]
    db.flush()
    _add_full_ocr_tokens(db, org, page)

    pack = load_pack_from_directory(PACK_DIR)
    publish_pack(db, pack)
    db.commit()

    _run_to_rule_eval(db, org, analysis, text=COMPLIANT_LABEL_JSON)
    for state in _PRE_RULE_EVAL_STATES:
        transition(db, analysis, state)
    assert _rule_eval(db, analysis) is None
    db.commit()
    transition(db, analysis, AnalysisState.SCORING)
    next_state = _scoring(db, analysis)
    transition(db, analysis, next_state or AnalysisState.COMPLETED)
    db.commit()
    db.refresh(analysis)
    return org, analysis


class TestRecordFindingDecision:
    def test_confirm_needs_no_reason(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        decision = service.record_finding_decision(
            db,
            organization_id=org.id,
            finding_id=finding.id,
            action=DecisionAction.CONFIRM,
            reason=None,
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label="reviewer@example.com",
        )
        db.commit()

        assert decision.action is DecisionAction.CONFIRM
        assert decision.reason is None
        assert decision.finding_id == finding.id
        assert decision.analysis_id == analysis.id

    def test_override_without_a_reason_is_blocked(self, db, basic) -> None:
        """The literal acceptance-criterion test line: "override without
        reason blocked"."""
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        with pytest.raises(ValidationFailed):
            service.record_finding_decision(
                db,
                organization_id=org.id,
                finding_id=finding.id,
                action=DecisionAction.OVERRIDE,
                reason=None,
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )

    def test_override_with_a_too_short_reason_is_blocked(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        with pytest.raises(ValidationFailed):
            service.record_finding_decision(
                db,
                organization_id=org.id,
                finding_id=finding.id,
                action=DecisionAction.OVERRIDE,
                reason="too short",  # < 20 chars
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )

    def test_override_with_a_real_reason_succeeds(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        decision = service.record_finding_decision(
            db,
            organization_id=org.id,
            finding_id=finding.id,
            action=DecisionAction.OVERRIDE,
            reason="The printed label is compliant despite this reading.",
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label=None,
        )
        db.commit()

        assert decision.action is DecisionAction.OVERRIDE
        assert decision.reason is not None

    def test_escalate_needs_no_reason(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        decision = service.record_finding_decision(
            db,
            organization_id=org.id,
            finding_id=finding.id,
            action=DecisionAction.ESCALATE,
            reason=None,
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label=None,
        )
        db.commit()

        assert decision.action is DecisionAction.ESCALATE

    def test_deciding_on_a_finding_from_another_org_is_not_found(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))
        other_org = make_org(db, name="Other Org")

        with pytest.raises(NotFound):
            service.record_finding_decision(
                db,
                organization_id=other_org.id,
                finding_id=finding.id,
                action=DecisionAction.CONFIRM,
                reason=None,
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )

    def test_list_finding_decisions_is_newest_first(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))

        first = service.record_finding_decision(
            db, organization_id=org.id, finding_id=finding.id, action=DecisionAction.CONFIRM,
            reason=None, actor_id=None, actor_type=ActorType.USER, actor_label=None,
        )
        db.commit()
        second = service.record_finding_decision(
            db, organization_id=org.id, finding_id=finding.id,
            action=DecisionAction.OVERRIDE,
            reason="Changed my mind after re-reading the actual label image.",
            actor_id=None, actor_type=ActorType.USER, actor_label=None,
        )
        db.commit()

        decisions = service.list_finding_decisions(
            db, organization_id=org.id, finding_id=finding.id
        )
        assert [d.id for d in decisions] == [second.id, first.id]


class TestCreateFieldCorrection:
    def test_fix_field_triggers_rule_only_reevaluation_and_creates_a_child_analysis(
        self, db, basic
    ) -> None:
        """The literal acceptance-criterion test line: "fix-field triggers
        rule-only re-evaluation and creates a child analysis"."""
        org, analysis = _compliant_analysis(db, basic)
        parent_finding_count = len(
            db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        )

        correction, child = service.create_field_correction(
            db,
            organization_id=org.id,
            analysis=analysis,
            field_path="dates.batch_number",
            corrected_value="B99999",
            reason="Batch number was misread by OCR.",
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label="reviewer@example.com",
        )
        db.commit()

        # A genuinely new, separate analysis - the parent is never edited.
        assert isinstance(correction, FieldCorrection)
        assert child.id != analysis.id
        assert child.parent_analysis_id == analysis.id
        assert correction.child_analysis_id == child.id
        assert correction.field_path == "dates.batch_number"
        assert correction.corrected_value == "B99999"
        assert correction.original_value == "B12345"

        # Rule-only re-evaluation genuinely ran to completion, synchronously.
        assert child.state in (AnalysisState.COMPLETED, AnalysisState.NEEDS_REVIEW)
        child_extraction = db.scalar(
            select(Extraction).where(Extraction.analysis_id == child.id)
        )
        assert child_extraction is not None
        assert child_extraction.payload["dates"]["batch_number"]["value"] == "B99999"

        child_findings = db.scalars(
            select(Finding).where(Finding.analysis_id == child.id)
        ).all()
        assert len(child_findings) == parent_finding_count  # every rule re-evaluated
        batch_finding = next(
            f for f in child_findings if f.rule_key == "IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"
        )
        assert batch_finding.status == "pass"  # the corrected value still satisfies the rule

        # The corrected field's evidence chain carries forward for real -
        # not fabricated, the original span's real page/bbox/tokens.
        child_field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == child_extraction.id,
                ExtractedField.field_path == "dates.batch_number",
            )
        )
        parent_extraction = db.scalar(
            select(Extraction).where(Extraction.analysis_id == analysis.id)
        )
        parent_field = db.scalar(
            select(ExtractedField).where(
                ExtractedField.extraction_id == parent_extraction.id,
                ExtractedField.field_path == "dates.batch_number",
            )
        )
        parent_span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == parent_field.id)
        )
        child_span = db.scalar(
            select(EvidenceSpan).where(EvidenceSpan.extracted_field_id == child_field.id)
        )
        assert child_span is not None
        assert child_span.file_page_id == parent_span.file_page_id
        assert (child_span.x1, child_span.y1, child_span.x2, child_span.y2) == (
            parent_span.x1, parent_span.y1, parent_span.x2, parent_span.y2,
        )

    def test_a_correction_that_breaks_the_rule_produces_a_real_fail_finding(
        self, db, basic
    ) -> None:
        """Not a rubber stamp: the child analysis genuinely re-evaluates -
        correcting a field to a blank value fails the real FSSAI rule that
        checks it, exactly as it would for any other blank declaration."""
        org, analysis = _compliant_analysis(db, basic)

        _correction, child = service.create_field_correction(
            db,
            organization_id=org.id,
            analysis=analysis,
            field_path="dates.batch_number",
            corrected_value="   ",
            reason="Testing the rule genuinely re-runs against the correction.",
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label=None,
        )
        db.commit()

        finding = db.scalar(
            select(Finding).where(
                Finding.analysis_id == child.id,
                Finding.rule_key == "IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED",
            )
        )
        assert finding.status == "fail"

    def test_a_field_with_no_prior_evidence_cannot_be_corrected(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        # Batch number never printed at all - no citation, no evidence span.
        broken = COMPLIANT_LABEL_JSON.replace(
            '"dates_batch_number": {"value": "B12345", "not_found_reason": null,\n'
            '                         "token_ids": [6], "confidence": 0.9}',
            '"dates_batch_number": {"value": null, "not_found_reason": "not printed",\n'
            '                         "token_ids": [], "confidence": 0.0}',
        )
        _run_to_rule_eval(db, org, analysis, text=broken)
        for state in _PRE_RULE_EVAL_STATES:
            transition(db, analysis, state)
        _rule_eval(db, analysis)
        db.commit()
        transition(db, analysis, AnalysisState.SCORING)
        next_state = _scoring(db, analysis)
        transition(db, analysis, next_state or AnalysisState.COMPLETED)
        db.commit()
        db.refresh(analysis)

        with pytest.raises(ValidationFailed, match="no existing evidence"):
            service.create_field_correction(
                db,
                organization_id=org.id,
                analysis=analysis,
                field_path="dates.batch_number",
                corrected_value="B00001",
                reason=None,
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )

    def test_an_uncorrectable_field_path_is_rejected(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)

        with pytest.raises(ValidationFailed):
            service.create_field_correction(
                db,
                organization_id=org.id,
                analysis=analysis,
                field_path="ingredients.items",  # list-shaped, not correctable
                corrected_value="anything",
                reason=None,
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )

    def test_correcting_an_analysis_still_mid_pipeline_is_rejected(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        # Stop right before rule_eval - the analysis has real extracted
        # fields but has not reached a reviewable stopping state yet.
        _extraction_for(db, org, analysis, text=COMPLIANT_LABEL_JSON)
        assert _evidence_verification(db, analysis) is None
        db.commit()
        assert _normalizing(db, analysis) is None
        db.commit()
        assert _classifying(db, analysis) is None
        db.commit()
        for state in _PRE_RULE_EVAL_STATES[:-1]:  # up to and including CLASSIFYING, not RULE_EVAL
            transition(db, analysis, state)
        db.commit()
        db.refresh(analysis)
        assert analysis.state is AnalysisState.CLASSIFYING

        with pytest.raises(ValidationFailed, match="in state"):
            service.create_field_correction(
                db,
                organization_id=org.id,
                analysis=analysis,
                field_path="dates.batch_number",
                corrected_value="B00001",
                reason=None,
                actor_id=None,
                actor_type=ActorType.USER,
                actor_label=None,
            )


class TestAssignReviewer:
    def test_assigns_and_clears_a_reviewer(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        reviewer = make_user(db, org, role=Role.REVIEWER)
        db.commit()

        service.assign_reviewer(
            db, organization_id=org.id, analysis=analysis, reviewer_id=reviewer.id
        )
        db.commit()
        db.refresh(analysis)
        assert analysis.assigned_reviewer_id == reviewer.id

        service.assign_reviewer(db, organization_id=org.id, analysis=analysis, reviewer_id=None)
        db.commit()
        db.refresh(analysis)
        assert analysis.assigned_reviewer_id is None


class TestListReviewQueue:
    def test_only_needs_review_and_review_states_are_queued(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        _run_to_rule_eval(db, org, analysis, text=COMPLIANT_LABEL_JSON)
        for state in _PRE_RULE_EVAL_STATES:
            transition(db, analysis, state)
        _rule_eval(db, analysis)
        db.commit()
        transition(db, analysis, AnalysisState.SCORING)
        _scoring(db, analysis)
        transition(db, analysis, AnalysisState.NEEDS_REVIEW)
        db.commit()

        entries = service.list_review_queue(db, organization_id=org.id)

        assert len(entries) == 1
        queued_analysis, sla_since = entries[0]
        assert queued_analysis.id == analysis.id
        assert sla_since is not None

    def test_a_completed_analysis_is_not_in_the_queue(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        assert analysis.state is AnalysisState.COMPLETED

        entries = service.list_review_queue(db, organization_id=org.id)

        assert entries == []


class TestSignOffAnalysis:
    def test_signs_off_a_completed_analysis_with_a_real_finding_set_hash(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        findings = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        assert len(findings) > 0

        signoff = service.sign_off_analysis(
            db,
            organization_id=org.id,
            analysis=analysis,
            actor_id=None,
            actor_type=ActorType.USER,
            actor_label="reviewer@example.com",
        )
        db.commit()

        assert signoff.analysis_id == analysis.id
        assert signoff.ruleset_version_id == analysis.ruleset_version_id
        assert signoff.finding_set_hash == service.compute_finding_set_hash(findings)

    def test_signing_off_twice_is_rejected(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        service.sign_off_analysis(
            db, organization_id=org.id, analysis=analysis, actor_id=None,
            actor_type=ActorType.USER, actor_label=None,
        )
        db.commit()

        with pytest.raises(Conflict):
            service.sign_off_analysis(
                db, organization_id=org.id, analysis=analysis, actor_id=None,
                actor_type=ActorType.USER, actor_label=None,
            )

    def test_cannot_sign_off_an_analysis_still_mid_pipeline(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page)

        with pytest.raises(ValidationFailed):
            service.sign_off_analysis(
                db, organization_id=org.id, analysis=analysis, actor_id=None,
                actor_type=ActorType.USER, actor_label=None,
            )

    def test_get_signoff_returns_none_before_sign_off(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        assert service.get_signoff(db, organization_id=org.id, analysis_id=analysis.id) is None

    def test_a_signed_off_analysis_is_read_only_for_decisions(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))
        service.sign_off_analysis(
            db, organization_id=org.id, analysis=analysis, actor_id=None,
            actor_type=ActorType.USER, actor_label=None,
        )
        db.commit()

        with pytest.raises(Conflict):
            service.record_finding_decision(
                db, organization_id=org.id, finding_id=finding.id,
                action=DecisionAction.CONFIRM, reason=None, actor_id=None,
                actor_type=ActorType.USER, actor_label=None,
            )

    def test_a_signed_off_analysis_is_read_only_for_corrections(self, db, basic) -> None:
        org, analysis = _compliant_analysis(db, basic)
        service.sign_off_analysis(
            db, organization_id=org.id, analysis=analysis, actor_id=None,
            actor_type=ActorType.USER, actor_label=None,
        )
        db.commit()

        with pytest.raises(Conflict):
            service.create_field_correction(
                db, organization_id=org.id, analysis=analysis,
                field_path="dates.batch_number", corrected_value="B00001", reason=None,
                actor_id=None, actor_type=ActorType.USER, actor_label=None,
            )
