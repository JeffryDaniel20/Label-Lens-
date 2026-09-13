"""P7-T3's eval harness, run for real: `evals.runner.run_dataset` drives the
actual golden dataset (`evals/dataset/golden_v1.json`) through the entire
real pipeline (`app.analysis.stages`) against the real, published
`in-fssai-food` v1.0.0 pack - the same posture every other pipeline test in
this codebase already takes, applied to the eval harness itself rather than
to a single hand-built analysis.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from app.rules.loader import load_pack_from_directory
from app.rules.models import Ruleset
from app.rules.schema import Severity
from evals.aggregate import aggregate
from evals.dataset import load_dataset
from evals.report import ModelManifest, render_report
from evals.runner import run_dataset
from tests.integration.test_fssai_pack_pipeline import PACK_DIR

pytestmark = pytest.mark.integration


class TestEvalDataset:
    def test_the_shipped_dataset_loads_and_has_the_expected_shape(self) -> None:
        dataset = load_dataset()
        assert dataset.version == "golden-v1"
        assert len(dataset.cases) == 6
        case_ids = {c.case_id for c in dataset.cases}
        assert "compliant_packaged_food" in case_ids
        assert "garbled_allergen_and_missing_batch" in case_ids
        for case in dataset.cases:
            assert case.expected_findings  # every case has real ground truth
            assert case.ocr_tokens


class TestRunDataset:
    def test_the_full_dataset_runs_through_the_real_pipeline_and_matches_ground_truth(
        self, db
    ) -> None:
        dataset = load_dataset()
        pack = load_pack_from_directory(PACK_DIR)

        results = run_dataset(db, dataset, pack)

        assert len(results) == 6
        by_id = {r.case_id for r in results}
        assert by_id == {c.case_id for c in dataset.cases}

        compliant = next(r for r in results if r.case_id == "compliant_packaged_food")
        assert compliant.actual_findings == compliant.expected_findings
        # Every one of the 12 fixed field paths `app.extraction.service`
        # always persists a row for (even an honestly-absent one, like
        # "claims.items": None here) - both sides carry the same 12 keys.
        assert compliant.actual_fields == compliant.expected_fields

        broken = next(r for r in results if r.case_id == "garbled_allergen_and_missing_batch")
        assert broken.actual_findings["IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED"] == "fail"
        assert broken.actual_findings["IN-FSSAI-FOOD-BATCH-NUMBER-DECLARED"] == "insufficient_data"

        misread = next(r for r in results if r.case_id == "model_misreads_net_quantity")
        # `ExtractedField.value_raw` keeps the model's own raw "999 kg"
        # claim forever (P3-T6's evidence gate never rewrites the row
        # itself, only `verified`/`match_ratio` - see
        # `app.extraction.evidence.verify_extraction`'s own docstring), but
        # the *fact* `_rule_eval` actually reads (`Extraction.payload`) was
        # demoted to missing, so the rule correctly can't see it either.
        assert misread.actual_fields.get("quantity.net_quantity") == "999 kg"
        assert misread.verified_flags  # at least one field was asserted
        assert False in misread.verified_flags  # the demoted citation
        assert misread.actual_findings["IN-FSSAI-FOOD-NET-QUANTITY-DECLARED"] == "insufficient_data"

    def test_aggregate_metrics_reflect_the_one_known_extraction_error(self, db) -> None:
        dataset = load_dataset()
        pack = load_pack_from_directory(PACK_DIR)
        results = run_dataset(db, dataset, pack)
        critical_rule_keys = frozenset(
            r.rule_key for r in pack.rules if r.severity == Severity.CRITICAL
        )

        summary = aggregate(results, critical_rule_keys=critical_rule_keys)

        assert summary.case_count == 6
        # Exactly one deliberately-injected field mismatch in the whole
        # dataset (`model_misreads_net_quantity`).
        assert summary.field_prf1.false_positives == 1
        assert summary.field_prf1.false_negatives == 1
        # Exactly one real violation in the whole dataset, correctly
        # flagged, no false alarms.
        assert summary.finding_prf1.true_positives == 1
        assert summary.finding_prf1.false_positives == 0
        assert summary.finding_prf1.false_negatives == 0
        assert summary.hallucination_rate is not None and summary.hallucination_rate > 0
        assert summary.human_review_rate is not None
        assert 0.0 <= summary.human_review_rate <= 1.0
        assert summary.override_rate is None
        assert "Not applicable" in summary.override_rate_note


class TestRenderReport:
    def test_the_report_renders_valid_looking_html_without_crashing(self, db) -> None:
        dataset = load_dataset()
        pack = load_pack_from_directory(PACK_DIR)
        results = run_dataset(db, dataset, pack)
        critical_rule_keys = frozenset(
            r.rule_key for r in pack.rules if r.severity == Severity.CRITICAL
        )
        summary = aggregate(results, critical_rule_keys=critical_rule_keys)
        ruleset = db.scalar(select(Ruleset))

        import datetime as dt

        manifest = ModelManifest(
            dataset_version=dataset.version,
            extraction_provider="eval-stub",
            extraction_model="eval-stub",
            prompt_version="1.0.0",
            ruleset_jurisdiction=ruleset.jurisdiction,
            ruleset_category=ruleset.category,
            ruleset_version=ruleset.version,
            generated_at=dt.datetime.now(dt.UTC),
        )

        html = render_report(summary, manifest)

        assert "<!doctype html>" in html
        assert "LabelLens golden-dataset eval report" in html
        assert "golden-v1" in html
        assert "Not applicable" in html


class TestCliMain:
    def test_make_eval_writes_a_versioned_report_tied_to_a_model_manifest(self, tmp_path) -> None:
        from evals import cli

        out_dir = tmp_path / "report"
        exit_code = cli.main(["--subset", "2", "--out-dir", str(out_dir)])

        assert exit_code == 0
        assert (out_dir / "report.html").exists()
        assert (out_dir / "report.json").exists()

        import json

        payload = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
        assert payload["dataset_version"] == "golden-v1"
        assert payload["case_count"] == 2
        assert payload["model_manifest"]["dataset_version"] == "golden-v1"
        assert payload["model_manifest"]["ruleset_jurisdiction"] == "IN"
