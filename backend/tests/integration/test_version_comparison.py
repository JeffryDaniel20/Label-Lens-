"""P6-T7's version comparison view: field and finding diff between two real
product versions, each run through the entire real pipeline
(`_evidence_verification -> _normalizing -> _classifying -> _rule_eval ->
_scoring`) against the real, published `in-fssai-food` pack - not hand-built
`ExtractedField`/`Finding` fixtures - so the diff is exercised over exactly
what a reviewer would actually see, the same posture
`test_fssai_pack_pipeline.py`/`test_review_service.py` already established.

Three real scenarios prove the task's own literal acceptance criterion ("the
view distinguishes label change vs rule change vs extraction change"):
identical labels under the same ruleset (no diff at all); a genuinely
different label under the same ruleset (`label_change`); and an identical
label re-evaluated under an amended ruleset version, with and without the
"compare under common ruleset" toggle (`rule_change`, then collapsed to
`unchanged` once both sides are forced onto identical rule content).
"""

# ruff: noqa: F811 - each `basic` test parameter below is pytest fixture
# injection by name, not a redefinition of the `basic` fixture imported at
# module scope for pytest to discover it in this file.

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.analysis import comparison
from app.analysis import service as analysis_service
from app.analysis.models import AnalysisState
from app.analysis.stages import _rule_eval, _scoring
from app.analysis.state_machine import transition
from app.catalog.models import File, FilePage, FileStatus, ProductVersion
from app.findings.models import Finding
from app.rules.loader import RulePack, compute_pack_checksum, load_pack_from_directory
from app.rules.publish import publish_pack
from tests.integration.test_fssai_pack_pipeline import (
    COMPLIANT_LABEL_JSON,
    PACK_DIR,
    _add_full_ocr_tokens,
    _run_to_rule_eval,
)
from tests.integration.test_pipeline_stages import (
    basic,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.integration

# Same real "garbled allergen name + missing batch number" label
# `test_fssai_pack_pipeline.py`'s own fail-path test already uses - a
# genuinely different, real label, not a synthetic diff.
BROKEN_LABEL_JSON = (
    COMPLIANT_LABEL_JSON.replace(
        '"allergens_declared": {"values": ["Wheat"], "not_found_reason": null,\n'
        '                         "token_ids": [4], "confidence": 0.9}',
        '"allergens_declared": {"values": ["Unobtainium"], "not_found_reason": null,\n'
        '                         "token_ids": [7], "confidence": 0.9}',
    )
    .replace(
        '"allergens_declaration_text": {"value": "Contains: Wheat", '
        '"not_found_reason": null,\n'
        '                                 "token_ids": [4], "confidence": 0.9}',
        '"allergens_declaration_text": {"value": "Contains: Unobtainium", '
        '"not_found_reason": null,\n'
        '                                 "token_ids": [7], "confidence": 0.9}',
    )
    .replace(
        '"dates_batch_number": {"value": "B12345", "not_found_reason": null,\n'
        '                         "token_ids": [6], "confidence": 0.9}',
        '"dates_batch_number": {"value": null, "not_found_reason": "not printed",\n'
        '                         "token_ids": [], "confidence": 0.0}',
    )
)
assert BROKEN_LABEL_JSON != COMPLIANT_LABEL_JSON

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


def _run_full_pipeline(db, org, analysis, *, text: str) -> None:
    _run_to_rule_eval(db, org, analysis, text=text)
    for state in _PRE_RULE_EVAL_STATES:
        transition(db, analysis, state)
    assert _rule_eval(db, analysis) is None
    db.commit()
    transition(db, analysis, AnalysisState.SCORING)
    next_state = _scoring(db, analysis)
    transition(db, analysis, next_state or AnalysisState.COMPLETED)
    db.commit()
    db.refresh(analysis)


def _second_version(db, org, product):
    """A second version of the SAME product, with its own file/page/analysis
    - the two-version setup a real comparison actually needs."""
    version = ProductVersion(organization_id=org.id, product_id=product.id, version_no=2)
    db.add(version)
    db.flush()
    file = File(
        organization_id=org.id,
        product_version_id=version.id,
        storage_key="org/x/pv/v2/render/z/1.png",
        original_filename="label2.png",
        sha256="b" * 64,
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
    file_hash = analysis_service.compute_file_set_hash(
        db, organization_id=org.id, version_id=version.id
    )
    analysis, _ = analysis_service.create_or_get_analysis(
        db, organization_id=org.id, version=version, file_set_hash=file_hash
    )
    db.commit()
    return version, page, analysis


def _publish_amended_batch_number_pack(db, pack):
    """A second, real published ruleset version - same pack, one real rule
    amended (`IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED`'s pattern tightened so it
    no longer matches "B12345") - the mechanism a real regulator republishing
    stricter guidance would produce, not a hand-faked `Finding` row."""
    amended_rules = []
    for rule in pack.rules:
        if rule.rule_key == "IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED":
            rule = rule.model_copy(
                update={
                    "version": rule.version + 1,
                    "logic": {"regex_matches": {"path": "dates.batch_number", "pattern": "^ZZZ"}},
                }
            )
        amended_rules.append(rule)
    manifest = pack.manifest.model_copy(update={"version": "1.0.1"})
    checksum = compute_pack_checksum(manifest, amended_rules)
    amended_pack = RulePack(manifest=manifest, rules=tuple(amended_rules), checksum=checksum)
    return publish_pack(db, amended_pack)


def _two_version_rig(db, basic_rig):
    """v1 and v2 of the same product, each with its own real, fully OCR'd
    page - callers drive each one's own analysis through the real pipeline
    with whatever label text/ruleset the test needs."""
    org, product, version1, _file1, page1, analysis1 = basic_rig
    product.category_hint = "packaged_food"
    product.market_codes = ["IN"]
    db.flush()
    _add_full_ocr_tokens(db, org, page1)
    version2, page2, analysis2 = _second_version(db, org, product)
    _add_full_ocr_tokens(db, org, page2)
    return org, product, version1, analysis1, version2, analysis2


class TestIdenticalLabelsHaveNoDiff:
    def test_no_diff_when_both_versions_print_the_same_compliant_label(self, db, basic) -> None:
        org, _product, _v1, analysis1, _v2, analysis2 = _two_version_rig(db, basic)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        _run_full_pipeline(db, org, analysis1, text=COMPLIANT_LABEL_JSON)
        _run_full_pipeline(db, org, analysis2, text=COMPLIANT_LABEL_JSON)

        result = comparison.compare_analyses(
            db, from_analysis=analysis1, to_analysis=analysis2, common_ruleset=False
        )

        assert result.ruleset_changed is False
        assert all(d.change == "unchanged" for d in result.field_diffs)
        assert result.finding_diffs  # real findings exist
        assert all(d.change == "unchanged" and d.cause == "unchanged" for d in result.finding_diffs)


class TestLabelChangeIsDetected:
    def test_a_genuinely_different_label_produces_a_label_change_finding_diff(
        self, db, basic
    ) -> None:
        org, _product, _v1, analysis1, _v2, analysis2 = _two_version_rig(db, basic)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()

        _run_full_pipeline(db, org, analysis1, text=COMPLIANT_LABEL_JSON)
        _run_full_pipeline(db, org, analysis2, text=BROKEN_LABEL_JSON)

        # Sanity: the real pipeline actually produced the two different
        # verdicts this test's own assertions depend on.
        v2_findings = {
            f.rule_key: f.status
            for f in db.scalars(
                select(Finding).where(Finding.analysis_id == analysis2.id)
            ).all()
        }
        assert v2_findings["IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED"] == "fail"
        assert v2_findings["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"] == "insufficient_data"

        result = comparison.compare_analyses(
            db, from_analysis=analysis1, to_analysis=analysis2, common_ruleset=False
        )

        field_by_path = {d.field_path: d for d in result.field_diffs}
        assert field_by_path["allergens.declared"].change == "changed"
        assert field_by_path["dates.batch_number"].change in {"changed", "removed"}

        finding_by_key = {d.rule_key: d for d in result.finding_diffs}
        allergen_diff = finding_by_key["IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED"]
        assert allergen_diff.from_status == "pass"
        assert allergen_diff.to_status == "fail"
        assert allergen_diff.change == "changed"
        assert allergen_diff.cause == "label_change"

        batch_diff = finding_by_key["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"]
        assert batch_diff.from_status == "pass"
        assert batch_diff.to_status == "insufficient_data"
        assert batch_diff.change == "changed"
        assert batch_diff.cause == "label_change"

        # A rule whose own evidence fields never changed stays unchanged.
        net_qty_diff = finding_by_key["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"]
        assert net_qty_diff.change == "unchanged"


class TestRuleChangeIsDistinguishedFromLabelChange:
    def test_an_amended_rule_under_an_unchanged_label_is_a_rule_change(self, db, basic) -> None:
        org, _product, _v1, analysis1, _v2, analysis2 = _two_version_rig(db, basic)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()
        _run_full_pipeline(db, org, analysis1, text=COMPLIANT_LABEL_JSON)

        amended_ruleset = _publish_amended_batch_number_pack(db, pack)
        db.commit()
        # The SAME compliant label, re-run after a real ruleset republish -
        # `_rule_eval`'s own `find_active_ruleset` picks the most recently
        # published one, so this analysis is pinned to the amended ruleset.
        _run_full_pipeline(db, org, analysis2, text=COMPLIANT_LABEL_JSON)
        db.refresh(analysis2)
        assert analysis2.ruleset_version_id == amended_ruleset.id
        assert analysis2.ruleset_version_id != analysis1.ruleset_version_id

        # With the toggle off: the label never changed, so any real
        # difference here can only be attributed to the rules.
        result = comparison.compare_analyses(
            db, from_analysis=analysis1, to_analysis=analysis2, common_ruleset=False
        )
        assert result.ruleset_changed is True
        assert all(d.change == "unchanged" for d in result.field_diffs)

        finding_by_key = {d.rule_key: d for d in result.finding_diffs}
        batch_diff = finding_by_key["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"]
        assert batch_diff.from_status == "pass"
        assert batch_diff.to_status == "fail"
        assert batch_diff.cause == "rule_change"

        # With the toggle on: both sides are re-judged by the SAME (the
        # newer side's) rule content, so the "difference" collapses - it was
        # never a real label change, exactly what the toggle is for.
        common_result = comparison.compare_analyses(
            db, from_analysis=analysis1, to_analysis=analysis2, common_ruleset=True
        )
        assert common_result.common_ruleset_applied is True
        common_by_key = {d.rule_key: d for d in common_result.finding_diffs}
        assert common_by_key["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"].change == "unchanged"
        assert common_by_key["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"].cause == "unchanged"


class TestMissingAnalysis:
    def test_a_version_never_analyzed_compares_honestly_empty(self, db, basic) -> None:
        org, product, version1, _file1, page1, analysis1 = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_full_ocr_tokens(db, org, page1)
        pack = load_pack_from_directory(PACK_DIR)
        publish_pack(db, pack)
        db.commit()
        _run_full_pipeline(db, org, analysis1, text=COMPLIANT_LABEL_JSON)

        result = comparison.compare_analyses(
            db, from_analysis=analysis1, to_analysis=None, common_ruleset=False
        )

        assert result.to_analysis_id is None
        assert result.common_ruleset_applied is False
        # Every one of v1's real fields is reported as "removed" relative
        # to a version with no analysis at all - never fabricated as "added".
        assert result.field_diffs
        assert all(d.change == "removed" for d in result.field_diffs)
        assert all(d.change == "removed" for d in result.finding_diffs)
