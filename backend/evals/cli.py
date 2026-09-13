"""`make eval` / `python -m evals.cli` (P7-T3): runs the golden dataset
through the real pipeline against a throwaway, run-scoped SQLite database
(created and torn down entirely inside this process - never the
application's own configured database, so this can run repeatedly, in CI
or locally, without ever touching real tenant data) and writes a versioned
JSON + HTML report under `evals/reports/`.

    python -m evals.cli                 # full dataset
    python -m evals.cli --subset 3      # first 3 cases only (the nightly-CI subset)
    python -m evals.cli --out-dir DIR   # write elsewhere (default: evals/reports/<timestamp>)
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

# Harmless defaults for a standalone run (mirrors tests/conftest.py's own
# pattern) - this CLI never calls `get_settings()` itself, but sets these
# before any app import in case something it imports does.
os.environ.setdefault("LABELLENS_ENVIRONMENT", "test")
os.environ.setdefault("LABELLENS_SECRET_KEY", "eval-harness-secret-key-that-is-long-enough-0")
os.environ.setdefault("LABELLENS_DATABASE_URL", "sqlite://")
os.environ.setdefault("LABELLENS_REDIS_URL", "memory://")

from sqlalchemy import select  # noqa: E402

from app.db.models import Base  # noqa: E402
from app.db.session import init_engine  # noqa: E402
from app.extraction.models import Extraction  # noqa: E402
from app.rules.loader import load_pack_from_directory  # noqa: E402
from app.rules.models import Ruleset  # noqa: E402
from app.rules.schema import Severity  # noqa: E402
from evals.aggregate import aggregate  # noqa: E402
from evals.dataset import load_dataset  # noqa: E402
from evals.report import ModelManifest, render_report  # noqa: E402
from evals.runner import run_dataset  # noqa: E402

_PACK_DIR = Path(__file__).resolve().parents[1] / "app" / "rulesets" / "in-fssai-food" / "v1.0.0"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--subset", type=int, default=None, help="run only the first N cases (nightly CI)"
    )
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="directory to write the report into"
    )
    args = parser.parse_args(argv)

    dataset = load_dataset()
    cases = dataset.cases[: args.subset] if args.subset else dataset.cases
    pack = load_pack_from_directory(_PACK_DIR)
    critical_rule_keys = frozenset(
        rule.rule_key for rule in pack.rules if rule.severity == Severity.CRITICAL
    )

    with tempfile.TemporaryDirectory(prefix="labellens-eval-") as tmp_dir:
        db_path = Path(tmp_dir) / "eval.db"
        engine = init_engine(f"sqlite:///{db_path}")
        Base.metadata.create_all(engine)
        from app.db.session import get_session_factory

        db = get_session_factory()()
        try:
            results = run_dataset(
                db, dataclasses.replace(dataset, cases=cases), pack
            )
            summary = aggregate(results, critical_rule_keys=critical_rule_keys)

            ruleset = db.scalar(
                select(Ruleset).where(
                    Ruleset.jurisdiction == pack.manifest.jurisdiction,
                    Ruleset.category == pack.manifest.category,
                )
            )
            first_extraction = db.scalar(select(Extraction).order_by(Extraction.created_at))
            manifest = ModelManifest(
                dataset_version=dataset.version,
                extraction_provider=first_extraction.provider if first_extraction else "n/a",
                extraction_model=first_extraction.model if first_extraction else "n/a",
                prompt_version=first_extraction.prompt_version if first_extraction else "n/a",
                ruleset_jurisdiction=(
                    ruleset.jurisdiction if ruleset else pack.manifest.jurisdiction
                ),
                ruleset_category=ruleset.category if ruleset else pack.manifest.category,
                ruleset_version=ruleset.version if ruleset else pack.manifest.version,
                generated_at=datetime.now(UTC),
            )
        finally:
            db.close()
            engine.dispose()

    out_dir = args.out_dir or (
        Path(__file__).resolve().parent
        / "reports"
        / manifest.generated_at.strftime("%Y%m%dT%H%M%SZ")
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    html = render_report(summary, manifest)
    (out_dir / "report.html").write_text(html, encoding="utf-8")

    manifest_dict = dataclasses.asdict(manifest) | {
        "generated_at": manifest.generated_at.isoformat()
    }
    summary_json = {
        "dataset_version": manifest.dataset_version,
        "generated_at": manifest.generated_at.isoformat(),
        "model_manifest": manifest_dict,
        "case_count": summary.case_count,
        "field_extraction": dataclasses.asdict(summary.field_prf1)
        | {
            "precision": summary.field_prf1.precision,
            "recall": summary.field_prf1.recall,
            "f1": summary.field_prf1.f1,
        },
        "finding_fail_detection": dataclasses.asdict(summary.finding_prf1)
        | {
            "precision": summary.finding_prf1.precision,
            "recall": summary.finding_prf1.recall,
            "f1": summary.finding_prf1.f1,
        },
        "finding_status_accuracy": summary.finding_status_accuracy,
        "hallucination_rate": summary.hallucination_rate,
        "human_review_rate": summary.human_review_rate,
        "critical_rule_false_negative_rate": summary.critical_rule_false_negative_rate,
        "override_rate_note": summary.override_rate_note,
        "expected_calibration_error": summary.expected_calibration_error,
    }
    (out_dir / "report.json").write_text(json.dumps(summary_json, indent=2), encoding="utf-8")

    print(f"Eval report written to {out_dir / 'report.html'}")
    print(
        f"cases={summary.case_count} "
        f"field_f1={summary.field_prf1.f1} "
        f"finding_f1={summary.finding_prf1.f1} "
        f"critical_fn_rate={summary.critical_rule_false_negative_rate}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
