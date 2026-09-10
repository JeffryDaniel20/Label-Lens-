"""The `rule_eval` pipeline stage (P5-T4, wired 2026-09-10): the real
orchestrator glue between P4-T3's evaluator, P4-T4's ruleset publishing, and
P5-T4's own findings persistence - not any of those three pieces' own logic,
which already have their own dedicated suites (`tests/unit/
test_rules_evaluator.py`, `tests/integration/test_rules_publish.py`,
`tests/integration/test_findings_service.py`).

Every ruleset/rule fixture below is exactly what those existing test suites
already use: a small, clearly-synthetic test rule ("IN-TEST-...", citation
"Test citation", author "test"), never real regulatory content - this file
tests the `rule_eval` stage's own wiring and idempotency in isolation from
any particular pack's content, which is exactly why synthetic content
(rather than the real `in-fssai-food` pack) is the right fixture here.

D-01 (first jurisdiction) was resolved to India/FSSAI by explicit user
instruction on 2026-09-10, and a real pack now exists on disk at
`app/rulesets/in-fssai-food/v1.0.0/` (P4-T5) - see
`tests/rules/test_in_fssai_food_pack.py` for that pack's own content tests,
and `tests/integration/test_fssai_pack_pipeline.py` for a real end-to-end
run of an analysis against it. `TestRuleEvalIsAnHonestNoOpToday` below
still holds and is still meaningful: it proves `rule_eval` behaves as a
correct no-op whenever a classified analysis's jurisdiction/category has no
*published* ruleset in that particular test's database session (the
default state for a fresh test database, since publishing is a deliberate
operator action - see `scripts/publish_ruleset.py` - never an implicit side
effect of running the app or its test suite).
"""

# ruff: noqa: F811 - each `basic` test parameter below is pytest fixture
# injection by name, not a redefinition of the `basic` fixture imported at
# module scope for pytest to discover it in this file.

from __future__ import annotations

import datetime as dt
import uuid

import pytest
from sqlalchemy import select

from app.analysis.retry_policy import PermanentStageError
from app.analysis.stages import _classifying, _evidence_verification, _normalizing, _rule_eval
from app.extraction.models import EvidenceSpan, ExtractedField, Extraction
from app.findings.models import Finding, FindingEvidence
from app.rules.loader import RulePack, compute_pack_checksum
from app.rules.models import Ruleset
from app.rules.publish import publish_pack
from app.rules.schema import PackManifest, Rule
from app.vision.models import OcrTokenRow
from tests.integration.test_pipeline_stages import (
    NORMALIZING_JSON,
    ONLY_QUANTITY_JSON,
    _add_ocr_tokens,
    _extraction_for,
    basic,  # noqa: F401 - pytest fixture
)

pytestmark = pytest.mark.integration

TODAY = dt.date(2026, 1, 1)


def _rule(**overrides: object) -> Rule:
    base: dict[str, object] = {
        "rule_key": "IN-TEST-NET-QTY",
        "version": 1,
        "title": "Test rule: net quantity must be declared",
        "citation": "Test citation, not real regulatory content",
        "severity": "major",
        "effective_from": "2020-01-01",
        "effective_to": None,
        "applicability": {
            "jurisdiction": ["IN"],
            "category": ["packaged_food"],
            "predicates": [],
        },
        "logic": {"field_present": "quantity.net_quantity"},
        "requires_fields": ["quantity.net_quantity"],
        "on_missing_fields": "insufficient_data",
        "message": {
            "fail": "Net quantity is not declared.",
            "insufficient_data": "Could not verify net quantity.",
        },
        "evidence": {"fields": ["quantity.net_quantity"]},
    }
    base.update(overrides)
    return Rule.model_validate(base)


def _publish_test_ruleset(
    db, *, version: str = "1.0.0", rules: list[Rule] | None = None
) -> Ruleset:
    """A real, published `Ruleset`/`RuleRow` via the real `publish_pack`
    path (not hand-inserted rows) - test-only content, same as every other
    rules-module test fixture in this codebase, never real regulation."""
    manifest = PackManifest(
        pack_id="in-test-food",
        jurisdiction="IN",
        category="packaged_food",
        version=version,
        effective_from=TODAY,
        effective_to=None,
        source_citations=["Test citation, not real regulatory content"],
        author="test",
        review_date=TODAY,
    )
    rule_list = rules or [_rule()]
    pack = RulePack(
        manifest=manifest,
        rules=tuple(rule_list),
        checksum=compute_pack_checksum(manifest, rule_list),
    )
    return publish_pack(db, pack)


def _run_through_rule_eval(db, org, analysis, *, text: str = NORMALIZING_JSON) -> Extraction:
    """The real chain up to and including `rule_eval` - every stage before
    it genuinely runs (only the LLM call itself is stubbed, via
    `_extraction_for`)."""
    extraction = _extraction_for(db, org, analysis, text=text)
    assert _evidence_verification(db, analysis) is None
    db.commit()
    assert _normalizing(db, analysis) is None
    db.commit()
    assert _classifying(db, analysis) is None
    db.commit()
    return extraction


class TestRuleEvalIsAnHonestNoOpToday:
    """The current, literal reality of this repository: no ruleset has ever
    been published for any jurisdiction (D-01 unresolved) - `rule_eval`
    must advance having done nothing, exactly like the placeholder it
    replaced, never fabricating a finding to fill the gap."""

    def test_no_published_ruleset_produces_no_findings(self, db, basic) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        _run_through_rule_eval(db, org, analysis)
        db.refresh(analysis)
        assert analysis.category == "packaged_food"  # classification genuinely succeeded

        result = _rule_eval(db, analysis)
        db.commit()

        assert result is None
        assert analysis.ruleset_version_id is None
        rows = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        assert rows == []

    def test_an_abstained_classification_is_skipped_without_a_ruleset_lookup(
        self, db, basic
    ) -> None:
        org, product, version, file, page, analysis = basic
        _add_ocr_tokens(db, org, page)
        # No hints, no address - a real, honest abstention (P3-T9).
        _run_through_rule_eval(db, org, analysis, text=ONLY_QUANTITY_JSON)
        db.refresh(analysis)
        assert analysis.category is None

        result = _rule_eval(db, analysis)
        db.commit()

        assert result is None
        assert analysis.ruleset_version_id is None
        rows = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        assert rows == []

    def test_a_missing_extraction_is_a_permanent_failure(self, db, basic) -> None:
        """The same defensive invariant every other real stage enforces -
        this should be unreachable via the real pipeline (`rule_eval` only
        ever runs after a real `extracting` committed), so a genuinely
        missing `Extraction` means something upstream broke."""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        analysis.category = "packaged_food"
        analysis.jurisdictions = ["IN"]
        db.flush()
        _publish_test_ruleset(db)

        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _rule_eval(db, analysis)


class TestRuleEvalWithARealPublishedRuleset:
    """Once a ruleset genuinely exists (test-only content here, exactly as
    it will be real content post-D-01), `rule_eval` must actually evaluate
    and persist - proving the wiring works, not just that it stays quiet
    when nothing is published."""

    def test_a_real_ruleset_produces_a_persisted_finding_with_its_full_evidence_chain(
        self, db, basic
    ) -> None:
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        # `NORMALIZING_JSON` cites token index 1 ("250 g") for
        # `quantity.net_quantity`, a real, matching, verifiable citation.
        _run_through_rule_eval(db, org, analysis)
        ruleset = _publish_test_ruleset(db)

        result = _rule_eval(db, analysis)
        db.commit()

        assert result is None
        db.refresh(analysis)
        # Ruleset/version pinning (app.rules.publish's own documented
        # guarantee): the analysis is now permanently tied to this exact
        # ruleset id, not "whatever is latest."
        assert analysis.ruleset_version_id == ruleset.id

        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))
        assert finding is not None
        assert finding.rule_key == "IN-TEST-NET-QTY"
        assert finding.rule_version == 1
        assert finding.ruleset_id == ruleset.id
        assert finding.status == "pass"  # quantity.net_quantity really is present

        # Full evidence chain: finding -> finding_evidence -> extracted_field
        # -> evidence_span -> file_page -> real OCR token / coordinates.
        edge = db.scalar(select(FindingEvidence).where(FindingEvidence.finding_id == finding.id))
        assert edge is not None
        field = db.get(ExtractedField, edge.extracted_field_id)
        assert field.field_path == "quantity.net_quantity"
        assert field.verified is True
        span = db.get(EvidenceSpan, edge.evidence_span_id)
        assert span.text_snippet == "250 g"
        token = db.get(OcrTokenRow, uuid.UUID(field.cited_token_ids[0]))
        assert token is not None
        assert token.text == "250 g"
        assert (span.x1, span.y1, span.x2, span.y2) == (
            token.x1,
            token.y1,
            token.x2,
            token.y2,
        )

    def test_a_field_p3_t6_demoted_forces_insufficient_data_not_a_guess(
        self, db, basic
    ) -> None:
        """A rule that `requires_fields` a value P3-T6 already rewrote to an
        explicit absence (a forged/hallucinated citation) must reach
        `insufficient_data` through the real engine, never `pass` or
        `fail` - the literal "AI extracts, rules decide, missing evidence
        never becomes a guess" chain, proven end to end."""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        # A forged, out-of-range citation for the required field - P3-T6's
        # own `_evidence_verification` demotes this to an explicit absence
        # before `rule_eval` ever runs.
        forged = NORMALIZING_JSON.replace('"token_ids": [1]', '"token_ids": [99]', 1)
        _run_through_rule_eval(db, org, analysis, text=forged)
        _publish_test_ruleset(db)

        _rule_eval(db, analysis)
        db.commit()

        finding = db.scalar(select(Finding).where(Finding.analysis_id == analysis.id))
        assert finding.status == "insufficient_data"
        assert finding.confidence == 0.0
        edges = db.scalars(
            select(FindingEvidence).where(FindingEvidence.finding_id == finding.id)
        ).all()
        assert edges == []  # nothing to cite - the field was demoted, not evidenced

    def test_rule_eval_is_reproducible_a_retry_never_duplicates_findings(
        self, db, basic
    ) -> None:
        """`Finding` rows are append-only (migration 0012) - a retried job
        after a crash between `persist_findings` committing and this
        stage's own transition committing must not create duplicates."""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        _run_through_rule_eval(db, org, analysis)
        _publish_test_ruleset(db)

        _rule_eval(db, analysis)
        db.commit()
        first_ids = sorted(
            str(f.id) for f in db.scalars(
                select(Finding).where(Finding.analysis_id == analysis.id)
            ).all()
        )
        assert len(first_ids) == 1

        # Simulate a retried job for the exact same analysis.
        result = _rule_eval(db, analysis)
        db.commit()

        second_ids = sorted(
            str(f.id) for f in db.scalars(
                select(Finding).where(Finding.analysis_id == analysis.id)
            ).all()
        )
        assert result is None
        assert second_ids == first_ids  # byte-identical - nothing duplicated

    def test_pinning_survives_a_later_republished_ruleset_version(self, db, basic) -> None:
        """Byte-identical re-evaluation (`app.rules.publish`'s own
        documented guarantee): an analysis judged against version 1.0.0
        stays pinned to it even after 2.0.0 is published for the same
        jurisdiction/category - "pins by id, never latest.\""""
        org, product, version, file, page, analysis = basic
        product.category_hint = "packaged_food"
        product.market_codes = ["IN"]
        db.flush()
        _add_ocr_tokens(db, org, page)
        _run_through_rule_eval(db, org, analysis)
        v1 = _publish_test_ruleset(db, version="1.0.0")

        _rule_eval(db, analysis)
        db.commit()
        db.refresh(analysis)
        assert analysis.ruleset_version_id == v1.id

        # A newer version is published later, for the same jurisdiction and
        # category, with different rule content.
        _publish_test_ruleset(
            db, version="2.0.0", rules=[_rule(rule_key="IN-TEST-NET-QTY-V2")]
        )

        db.refresh(analysis)
        assert analysis.ruleset_version_id == v1.id  # unchanged - pinned, not "latest"
        rows = db.scalars(select(Finding).where(Finding.analysis_id == analysis.id)).all()
        assert all(f.ruleset_id == v1.id for f in rows)
