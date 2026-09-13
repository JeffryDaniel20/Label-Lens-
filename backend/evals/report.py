"""Renders one `EvalSummary` (plus the model manifest it was measured
against) as a self-contained HTML report - `make eval`'s own literal
acceptance-criterion output. Every piece of dynamic content is passed
through stdlib `html.escape()` before interpolation, the same untrusted-data
discipline `app.reports.pdf`'s HTML rendering already established for
label-derived text.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from html import escape

from evals.aggregate import EvalSummary


@dataclass(frozen=True, slots=True)
class ModelManifest:
    """What a report's numbers are tied to (this task's own acceptance
    line: "a versioned report tied to a model manifest") - real values read
    back from what the run actually used, never hardcoded."""

    dataset_version: str
    extraction_provider: str
    extraction_model: str
    prompt_version: str
    ruleset_jurisdiction: str
    ruleset_category: str
    ruleset_version: str
    generated_at: dt.datetime


def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _fmt_num(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.3f}"


def render_report(summary: EvalSummary, manifest: ModelManifest) -> str:
    field = summary.field_prf1
    finding = summary.finding_prf1

    calibration_rows = "".join(
        f"<tr><td>{escape(f'{b.lower:.1f}-{b.upper:.1f}')}</td>"
        f"<td>{b.count}</td>"
        f"<td>{_fmt_num(b.avg_confidence)}</td>"
        f"<td>{_fmt_num(b.accuracy)}</td></tr>"
        for b in summary.calibration_bins
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>LabelLens eval report - {escape(manifest.dataset_version)}</title>
<style>
  body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #1e293b; }}
  h1, h2 {{ color: #0f172a; }}
  table {{ border-collapse: collapse; margin: 0.5rem 0 1.5rem; }}
  th, td {{ border: 1px solid #cbd5e1; padding: 0.4rem 0.8rem; text-align: left; }}
  th {{ background: #f1f5f9; }}
  .manifest {{ background: #f8fafc; border: 1px solid #cbd5e1; padding: 1rem; border-radius: 6px; }}
  .note {{ color: #64748b; font-size: 0.9rem; }}
</style>
</head>
<body>
<h1>LabelLens golden-dataset eval report</h1>

<h2>Model manifest</h2>
<div class="manifest">
  <p>Dataset version: <strong>{escape(manifest.dataset_version)}</strong></p>
  <p>Extraction provider/model:
    <strong>{escape(manifest.extraction_provider)} /
    {escape(manifest.extraction_model)}</strong></p>
  <p>Prompt version: <strong>{escape(manifest.prompt_version)}</strong></p>
  <p>Ruleset: <strong>{escape(manifest.ruleset_jurisdiction)}/{escape(manifest.ruleset_category)}
    v{escape(manifest.ruleset_version)}</strong></p>
  <p>Generated at: <strong>{escape(manifest.generated_at.isoformat())}</strong></p>
  <p>Cases evaluated: <strong>{summary.case_count}</strong></p>
</div>

<h2>Extraction accuracy (field-level, exact-match on raw asserted value)</h2>
<table>
  <tr><th>Precision</th><th>Recall</th><th>F1</th><th>TP</th><th>FP</th><th>FN</th></tr>
  <tr>
    <td>{_fmt_num(field.precision)}</td>
    <td>{_fmt_num(field.recall)}</td>
    <td>{_fmt_num(field.f1)}</td>
    <td>{field.true_positives}</td>
    <td>{field.false_positives}</td>
    <td>{field.false_negatives}</td>
  </tr>
</table>
<p class="note">Hallucination rate (asserted fields with no supporting evidence):
  <strong>{_fmt_pct(summary.hallucination_rate)}</strong></p>

<h2>Compliance-decision accuracy (finding-level, fail-detection)</h2>
<table>
  <tr><th>Precision</th><th>Recall</th><th>F1</th><th>TP</th><th>FP</th><th>FN</th></tr>
  <tr>
    <td>{_fmt_num(finding.precision)}</td>
    <td>{_fmt_num(finding.recall)}</td>
    <td>{_fmt_num(finding.f1)}</td>
    <td>{finding.true_positives}</td>
    <td>{finding.false_positives}</td>
    <td>{finding.false_negatives}</td>
  </tr>
</table>
<p class="note">Overall verdict-status accuracy
  (multi-class, pass/fail/insufficient_data/not_applicable):
  <strong>{_fmt_pct(summary.finding_status_accuracy)}</strong></p>
<p class="note">Critical-rule false-negative rate
  (the headline safety metric):
  <strong>{_fmt_pct(summary.critical_rule_false_negative_rate)}</strong></p>

<h2>Human review rate</h2>
<p><strong>{_fmt_pct(summary.human_review_rate)}</strong> of cases did not reach
  `high` confidence and would route to human review.</p>

<h2>Override rate</h2>
<p class="note">{escape(summary.override_rate_note)}</p>

<h2>Calibration (predicted confidence vs observed accuracy)</h2>
<table>
  <tr><th>Confidence bin</th><th>Count</th><th>Avg. confidence</th><th>Observed accuracy</th></tr>
  {calibration_rows}
</table>
<p class="note">Expected calibration error (ECE):
  <strong>{_fmt_num(summary.expected_calibration_error)}</strong></p>

</body>
</html>
"""
