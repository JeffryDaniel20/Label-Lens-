"""HTML template + WeasyPrint PDF rendering (P7-T2).

`render_html` builds the printable version of the same JSON snapshot
`app.reports.service.build_snapshot` already assembles - genuinely "the same
HTML template" IMPLEMENTATION.md section 19 calls for, not a second,
independently-maintained report shape. Every piece of snapshot content that
ultimately derives from label text (product/org names, rule messages,
extracted values) is HTML-escaped before interpolation - the same
untrusted-data discipline P3-T5's prompt framing already applies to LLM
input applies here too: nothing printed on a label should be able to break
out of the generated HTML.

`render_pdf` is the one function that actually needs WeasyPrint's native
Pango/cairo/gdk-pixbuf libraries - imported lazily inside the function body,
exactly like `app.extraction.llm.gemini`'s SDK import and
`app.vision.ocr.paddle`'s PaddleOCR import, so importing this module (or
anything that imports it) never requires those libraries to be installed.
"""

from __future__ import annotations

from html import escape
from typing import Any


def _esc(value: object) -> str:
    return escape(str(value)) if value is not None else ""


def render_html(snapshot: dict[str, Any]) -> str:
    cover = snapshot["cover"]
    verdict = snapshot["verdict_summary"]
    provenance = snapshot["provenance"]

    body = "\n".join(
        (
            _cover_section(cover, verdict),
            _provenance_section(provenance),
            _findings_section("Findings", snapshot["findings"]),
            _findings_section("Not Applicable Rules", snapshot["not_applicable_rules"]),
            _findings_section("Insufficient Data", snapshot["insufficient_data"]),
            _appendix_section(snapshot["appendix"]),
        )
    )
    style = (
        "body{font-family:sans-serif;font-size:11px;}"
        "h1{font-size:20px;} h2{font-size:15px;margin-top:24px;}"
        "table{border-collapse:collapse;width:100%;margin-bottom:12px;}"
        "th,td{border:1px solid #ccc;padding:4px 6px;text-align:left;vertical-align:top;}"
        ".evidence-crop{max-width:200px;max-height:120px;}"
    )
    return (
        f'<!doctype html><html><head><meta charset="utf-8"><style>{style}</style>'
        f"</head><body>{body}</body></html>"
    )


def _cover_section(cover: dict[str, Any], verdict: dict[str, Any]) -> str:
    return f"""
    <h1>Compliance Report</h1>
    <table>
      <tr><th>Organization</th><td>{_esc(cover.get("organization_name"))}</td></tr>
      <tr><th>Product</th><td>{_esc(cover.get("product_name"))}</td></tr>
      <tr><th>Analysis ID</th><td>{_esc(cover.get("analysis_id"))}</td></tr>
      <tr><th>Generated at</th><td>{_esc(cover.get("generated_at"))}</td></tr>
      <tr><th>Report hash</th><td>{_esc(cover.get("report_hash"))}</td></tr>
      <tr><th>Overall status</th><td>{_esc(verdict.get("overall_status"))}</td></tr>
      <tr><th>Confidence tier</th><td>{_esc(verdict.get("confidence_tier"))}</td></tr>
      <tr><th>Counts by severity</th><td>{_esc(verdict.get("counts_by_severity"))}</td></tr>
    </table>
    """


def _provenance_section(provenance: dict[str, Any]) -> str:
    ruleset = provenance.get("ruleset") or {}
    manifest = provenance.get("model_manifest") or {}
    files = provenance.get("file_checksums") or []
    file_rows = "".join(
        f"<tr><td>{_esc(f.get('filename'))}</td><td>{_esc(f.get('sha256'))}</td></tr>"
        for f in files
    )
    ruleset_label = (
        f"{_esc(ruleset.get('jurisdiction'))} / {_esc(ruleset.get('category'))} "
        f"v{_esc(ruleset.get('version'))}"
        if ruleset
        else "-"
    )
    extractor_label = (
        f"{_esc(manifest.get('extractor_provider'))} / {_esc(manifest.get('extractor_model'))}"
    )
    return f"""
    <h2>Provenance</h2>
    <table>
      <tr><th>Ruleset</th><td>{ruleset_label}</td></tr>
      <tr><th>OCR engines</th><td>{_esc(", ".join(manifest.get("ocr_engines", [])))}</td></tr>
      <tr><th>Extractor</th><td>{extractor_label}</td></tr>
      <tr><th>Normalizer</th><td>{_esc(manifest.get("normalizer_version"))}</td></tr>
      <tr><th>Classifier</th><td>{_esc(manifest.get("classifier_version"))}</td></tr>
    </table>
    <table><tr><th>File</th><th>SHA-256</th></tr>{file_rows}</table>
    """


def _findings_section(title: str, findings: list[dict[str, Any]]) -> str:
    if not findings:
        return f"<h2>{_esc(title)}</h2><p>None.</p>"
    rows = []
    for finding in findings:
        evidence_html = "".join(
            f'<div><img class="evidence-crop" '
            f'src="data:image/png;base64,{evidence["image_base64"]}">'
            f"<div>{_esc(evidence.get('text_snippet'))}</div></div>"
            for evidence in finding.get("evidence", [])
        )
        rows.append(
            f"<tr><td>{_esc(finding.get('rule_key'))} v{_esc(finding.get('rule_version'))}</td>"
            f"<td>{_esc(finding.get('severity'))}</td><td>{_esc(finding.get('status'))}</td>"
            f"<td>{_esc(finding.get('message') or finding.get('reason'))}</td>"
            f"<td>{_esc(finding.get('citation'))}</td>"
            f"<td>{evidence_html}</td></tr>"
        )
    return (
        f"<h2>{_esc(title)}</h2><table>"
        "<tr><th>Rule</th><th>Severity</th><th>Status</th><th>Message</th>"
        "<th>Citation</th><th>Evidence</th></tr>" + "".join(rows) + "</table>"
    )


def _appendix_section(appendix: dict[str, Any]) -> str:
    fields = appendix.get("extracted_fields") or []
    field_rows = "".join(
        f"<tr><td>{_esc(f.get('field_path'))}</td><td>{_esc(f.get('value_raw'))}</td>"
        f"<td>{_esc(f.get('confidence'))}</td><td>{_esc(f.get('verified'))}</td></tr>"
        for f in fields
    )
    pages = appendix.get("ocr_text_by_page") or []
    page_rows = "".join(
        f"<tr><td>{_esc(page.get('page_no'))}</td><td>{_esc(page.get('text'))}</td></tr>"
        for page in pages
    )
    return (
        "<h2>Appendix: Extracted Fields</h2>"
        "<table><tr><th>Field</th><th>Value</th><th>Confidence</th><th>Verified</th></tr>"
        f"{field_rows}</table>"
        "<h2>Appendix: OCR Text by Page</h2>"
        f"<table><tr><th>Page</th><th>Text</th></tr>{page_rows}</table>"
    )


def render_pdf(html: str) -> bytes:
    from weasyprint import HTML

    pdf: bytes = HTML(string=html).write_pdf()
    return pdf
