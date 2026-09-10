"""Integration tests for P3-T8's DB-touching half: `compute_analysis_tier`
aggregating real `ExtractedField`/`OcrTokenRow` rows, and the `_scoring`
stage function that persists the result onto `Analysis.confidence_tier` and
routes to `needs_review` or lets `completed` fall through. The pure
confidence math has its own unit suite in
`tests/unit/test_confidence_tiers.py`.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from app.analysis.models import Analysis, AnalysisState, ConfidenceTier
from app.analysis.retry_policy import PermanentStageError
from app.analysis.stages import _scoring
from app.catalog.models import File, FilePage, FileStatus, Product, ProductVersion
from app.confidence.tiers import compute_analysis_tier
from app.extraction.models import ExtractedField, Extraction
from app.findings.models import Finding
from app.rules.evaluator import FindingStatus
from app.rules.models import RuleRow, Ruleset
from app.rules.schema import Severity
from app.vision.models import OcrResult, OcrTokenRow
from tests.conftest import make_org

pytestmark = pytest.mark.integration


@pytest.fixture
def rig(db):
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
    page = FilePage(
        organization_id=org.id, file_id=file.id, page_no=1, width=10, height=10, render_key="r"
    )
    db.add(page)
    db.flush()
    analysis = Analysis(
        organization_id=org.id,
        product_version_id=version.id,
        state=AnalysisState.SCORING,
        idempotency_key=uuid.uuid4().hex,
        # A confidently-*resolved* classification by default (both signals
        # at their ceiling) - these tests are about field/OCR/extraction
        # confidence, not classification, so the fixture simulates
        # `_classifying` having already succeeded with full confidence
        # rather than leaving `category=None` (an abstention, which would
        # force every one of these tests to Low regardless of their own
        # fields - see `TestClassificationSignal` for that dimension).
        category="packaged_food",
        jurisdictions=["IN"],
        category_confidence=1.0,
        jurisdiction_confidence=1.0,
    )
    db.add(analysis)
    db.flush()
    extraction = Extraction(
        organization_id=org.id,
        analysis_id=analysis.id,
        schema_version="1.0.0",
        payload={},
        envelope={},
        provider="stub",
        model="stub",
        prompt_version="1",
        prompt_hash="h",
    )
    db.add(extraction)
    db.flush()
    return org, page, analysis, extraction


def _token(db, org, page, *, confidence: float) -> str:
    result = OcrResult(
        organization_id=org.id,
        file_page_id=page.id,
        engine="stub",
        engine_version="1",
        avg_confidence=confidence,
        raw=[],
    )
    db.add(result)
    db.flush()
    token = OcrTokenRow(
        organization_id=org.id,
        file_page_id=page.id,
        ocr_result_id=result.id,
        text="x",
        confidence=confidence,
        x1=0.0, y1=0.0, x2=1.0, y2=1.0,
        line_no=0,
    )
    db.add(token)
    db.flush()
    return str(token.id)


def _field(
    db, org, extraction, *, field_path: str, value_raw: str | None,
    confidence: float = 0.9, verified: bool | None = None,
    cited_token_ids: list[str] | None = None,
) -> ExtractedField:
    field = ExtractedField(
        organization_id=org.id,
        extraction_id=extraction.id,
        field_path=field_path,
        value_raw=value_raw,
        confidence=confidence,
        verified=verified,
        cited_token_ids=cited_token_ids or [],
    )
    db.add(field)
    db.flush()
    return field


class TestComputeAnalysisTier:
    def test_all_fields_verified_and_confident_yields_high(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[tok],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.tier is ConfidenceTier.HIGH
        assert result.fields[0].confidence == pytest.approx(0.95)

    def test_a_medium_band_field_caps_the_whole_analysis_at_medium(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        high_tok = _token(db, org, page, confidence=0.95)
        medium_tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[high_tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.8, verified=True, cited_token_ids=[medium_tok],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.tier is ConfidenceTier.MEDIUM

    def test_a_missing_field_forces_low_even_with_other_high_confidence_fields(
        self, db, rig
    ) -> None:
        """The acceptance criterion's own test line: a missing rule-relevant
        field forces Low."""
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw=None,
            confidence=0.0, verified=None,
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.tier is ConfidenceTier.LOW
        missing = next(f for f in result.fields if f.field_path == "dates.batch_number")
        assert missing.reason == "field was not found on the label"

    def test_a_demoted_field_forces_low(self, db, rig) -> None:
        """A field P3-T6 demoted (hallucination/uncited) must never be
        rescued by an otherwise-high extraction confidence."""
        org, page, _analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.99, verified=False,
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.tier is ConfidenceTier.LOW
        demoted = next(f for f in result.fields if f.field_path == "dates.batch_number")
        assert demoted.reason == "field failed evidence verification"

    def test_field_confidence_uses_the_worst_cited_token_not_the_average(self, db, rig) -> None:
        org, page, _analysis, extraction = rig
        strong = _token(db, org, page, confidence=0.99)
        weak = _token(db, org, page, confidence=0.5)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[strong, weak],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.fields[0].confidence == pytest.approx(0.5)
        assert result.tier is ConfidenceTier.LOW

    def test_no_extracted_fields_at_all_is_low(self, db, rig) -> None:
        _org, _page, _analysis, extraction = rig

        result = compute_analysis_tier(db, extraction=extraction, analysis=_analysis)

        assert result.tier is ConfidenceTier.LOW
        assert result.fields[0].reason == "no fields were extracted"


def _finding(
    db, org, analysis, ruleset, *, rule_key: str, status: FindingStatus, evidence_fields: list[str]
) -> Finding:
    rule_row = RuleRow(
        ruleset_id=ruleset.id,
        rule_key=rule_key,
        version=1,
        title="t",
        citation="c",
        severity=Severity.MAJOR.value,
        effective_from=dt.date(2024, 1, 1),
        payload={},
    )
    db.add(rule_row)
    db.flush()
    finding = Finding(
        organization_id=org.id,
        analysis_id=analysis.id,
        ruleset_id=ruleset.id,
        rule_id=rule_row.id,
        rule_key=rule_key,
        rule_version=1,
        status=status,
        severity=Severity.MAJOR,
        details={"reason": None, "evidence_fields": evidence_fields},
        confidence=0.0,
    )
    db.add(finding)
    db.flush()
    return finding


class TestRuleRelevantFieldsNarrowTheTier:
    """2026-09-10: with `rule_eval` real (P5-T4) and a real ruleset published
    (P4-T5/D-01), `compute_analysis_tier` narrows to exactly the fields real
    findings named as relevant - this module's own long-documented "next
    step," taken. Every test here persists real `Finding` rows directly
    (not through `persist_findings`, which additionally needs real evidence
    spans this test doesn't care about) - `compute_analysis_tier` only ever
    reads `Finding.status`/`Finding.details`, proven by testing against
    exactly that surface."""

    def _ruleset(self, db) -> Ruleset:
        ruleset = Ruleset(
            jurisdiction="IN",
            category="packaged_food",
            version="1.0.0",
            effective_from=dt.date(2024, 1, 1),
            source_citations=["test"],
            author="test",
            review_date=dt.date(2024, 1, 1),
            checksum=uuid.uuid4().hex,
        )
        db.add(ruleset)
        db.flush()
        return ruleset

    def test_a_field_no_finding_depends_on_no_longer_drags_the_tier_down(
        self, db, rig
    ) -> None:
        org, page, analysis, extraction = rig
        ruleset = self._ruleset(db)
        high_tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[high_tok],
        )
        # A genuinely low-confidence field that no rule in this ruleset
        # depends on - e.g. extracted for a different, unpublished pack.
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.1, verified=True,
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-NET-QUANTITY-DECLARED", status=FindingStatus.PASS,
            evidence_fields=["quantity.net_quantity"],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.HIGH
        assert {f.field_path for f in result.fields} == {
            "quantity.net_quantity", "classification.category", "classification.jurisdiction",
        }

    def test_a_field_a_finding_does_depend_on_still_forces_low_when_missing(
        self, db, rig
    ) -> None:
        org, page, analysis, extraction = rig
        ruleset = self._ruleset(db)
        tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw=None,
            confidence=0.0, verified=None,
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-NET-QUANTITY-DECLARED", status=FindingStatus.PASS,
            evidence_fields=["quantity.net_quantity"],
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED", status=FindingStatus.INSUFFICIENT_DATA,
            evidence_fields=["dates.batch_number"],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.LOW

    def test_not_applicable_findings_do_not_make_their_fields_relevant(
        self, db, rig
    ) -> None:
        """A `not_applicable` finding's `evidence_fields` name what it *would*
        have checked under a different jurisdiction/category - never fields
        this label was actually judged against. A low-confidence field named
        only by a `not_applicable` finding must stay irrelevant, not force
        the tier down."""
        org, page, analysis, extraction = rig
        ruleset = self._ruleset(db)
        high_tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[high_tok],
        )
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw="B1",
            confidence=0.1, verified=True,
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-NET-QUANTITY-DECLARED", status=FindingStatus.PASS,
            evidence_fields=["quantity.net_quantity"],
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED", status=FindingStatus.NOT_APPLICABLE,
            evidence_fields=["dates.batch_number"],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.HIGH
        assert "dates.batch_number" not in {f.field_path for f in result.fields}

    def test_findings_that_are_all_not_applicable_fall_back_to_the_full_superset(
        self, db, rig
    ) -> None:
        """Narrowing to an empty relevant-field set would silently drop every
        field from consideration - never safe - so this behaves exactly like
        "no findings at all"."""
        org, page, analysis, extraction = rig
        ruleset = self._ruleset(db)
        _field(
            db, org, extraction, field_path="dates.batch_number", value_raw=None,
            confidence=0.0, verified=None,
        )
        _finding(
            db, org, analysis, ruleset,
            rule_key="IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED", status=FindingStatus.NOT_APPLICABLE,
            evidence_fields=["dates.batch_number"],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.LOW
        assert any(f.field_path == "dates.batch_number" for f in result.fields)


class TestClassificationSignalAffectsTier:
    """P3-T8's own objective, taken literally: classification (P3-T9) is a
    third real signal in the final tier, not just OCR/extraction - an
    abstained or weakly-resolved classification must degrade the analysis
    tier exactly like a missing/demoted field does, and never be silently
    ignored just because it lives on `Analysis` rather than
    `ExtractedField`."""

    def test_an_abstained_classification_forces_low_even_with_perfect_fields(
        self, db, rig
    ) -> None:
        org, page, analysis, extraction = rig
        analysis.category = None
        analysis.jurisdictions = []
        analysis.category_confidence = 0.33
        analysis.jurisdiction_confidence = 0.0
        db.flush()
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.LOW
        category_result = next(
            f for f in result.fields if f.field_path == "classification.category"
        )
        jurisdiction_result = next(
            f for f in result.fields if f.field_path == "classification.jurisdiction"
        )
        assert category_result.tier is ConfidenceTier.LOW
        assert category_result.confidence == pytest.approx(0.33)
        assert "abstained" in category_result.reason
        assert jurisdiction_result.tier is ConfidenceTier.LOW
        assert "abstained" in jurisdiction_result.reason

    def test_a_resolved_but_partial_confidence_classification_caps_at_medium(
        self, db, rig
    ) -> None:
        """A real, non-abstained classification (e.g. jurisdiction inferred
        from an address keyword match rather than an exact declared market
        code) is a weaker basis than a perfect one - it must not authorize
        full auto-completion just because every field happens to read
        perfectly."""
        org, page, analysis, extraction = rig
        analysis.category_confidence = 1.0
        analysis.jurisdiction_confidence = 0.6  # a real, passing, but partial signal
        db.flush()
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.MEDIUM
        jurisdiction_result = next(
            f for f in result.fields if f.field_path == "classification.jurisdiction"
        )
        assert jurisdiction_result.tier is ConfidenceTier.MEDIUM
        assert jurisdiction_result.reason is None  # not forced - a real, graded outcome

    def test_full_confidence_classification_alongside_perfect_fields_allows_high(
        self, db, rig
    ) -> None:
        """The positive case: nothing about combining a third signal makes
        High unreachable when every signal genuinely earns it."""
        org, page, analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.HIGH

    def test_classification_confidence_boundary_at_the_high_ceiling(self, db, rig) -> None:
        """Exactly 1.0 (every available classification signal agreed) is
        the one value High-eligible; anything even marginally short of it
        is a real but incomplete signal and caps at Medium - a deliberate,
        documented boundary (see the module docstring), not the same
        0.90/0.70 bands the per-field confidences use."""
        org, page, analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.99)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.99, verified=True, cited_token_ids=[tok],
        )

        analysis.category_confidence = 0.999
        db.flush()
        just_below = compute_analysis_tier(db, extraction=extraction, analysis=analysis)
        assert just_below.tier is ConfidenceTier.MEDIUM

        analysis.category_confidence = 1.0
        db.flush()
        at_ceiling = compute_analysis_tier(db, extraction=extraction, analysis=analysis)
        assert at_ceiling.tier is ConfidenceTier.HIGH

    def test_conflicting_signals_a_perfect_classification_cannot_rescue_a_bad_field(
        self, db, rig
    ) -> None:
        """The reverse conflict: an excellent classification must not
        dilute away a genuinely bad field, matching the same worst-tier-wins
        rule already enforced among fields themselves."""
        org, page, analysis, extraction = rig
        _field(db, org, extraction, field_path="quantity.net_quantity", value_raw=None)

        result = compute_analysis_tier(db, extraction=extraction, analysis=analysis)

        assert result.tier is ConfidenceTier.LOW


class TestScoringStage:
    def test_a_high_tier_extraction_falls_through_to_the_default_successor(
        self, db, rig
    ) -> None:
        org, page, analysis, extraction = rig
        tok = _token(db, org, page, confidence=0.95)
        _field(
            db, org, extraction, field_path="quantity.net_quantity", value_raw="250 g",
            confidence=0.95, verified=True, cited_token_ids=[tok],
        )

        next_state = _scoring(db, analysis)
        db.commit()

        assert next_state is None
        db.refresh(analysis)
        assert analysis.confidence_tier is ConfidenceTier.HIGH

    def test_a_low_tier_extraction_routes_explicitly_to_needs_review(self, db, rig) -> None:
        org, page, analysis, extraction = rig
        _field(db, org, extraction, field_path="quantity.net_quantity", value_raw=None)

        next_state = _scoring(db, analysis)
        db.commit()

        assert next_state is AnalysisState.NEEDS_REVIEW
        db.refresh(analysis)
        assert analysis.confidence_tier is ConfidenceTier.LOW

    def test_no_extraction_is_a_permanent_failure(self, db, rig) -> None:
        """A fresh analysis that never reached `extracting` at all - not the
        fixture's own analysis, which already has an `Extraction` row."""
        _org, _page, analysis, _extraction = rig
        bare = Analysis(
            organization_id=analysis.organization_id,
            product_version_id=analysis.product_version_id,
            state=AnalysisState.SCORING,
            idempotency_key=uuid.uuid4().hex,
        )
        db.add(bare)
        db.flush()

        with pytest.raises(PermanentStageError, match="No extraction exists"):
            _scoring(db, bare)
