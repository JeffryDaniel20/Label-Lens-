"""Unit tests for P7-T2's HTML template (pure - no native libs needed) and,
where WeasyPrint's native Pango/cairo/gdk-pixbuf libraries are actually
importable, the real PDF rendering call.
"""

from __future__ import annotations

import pytest

from app.reports.pdf import render_html

pytestmark = pytest.mark.unit

try:
    import weasyprint  # noqa: F401

    _WEASYPRINT_AVAILABLE = True
except OSError:
    _WEASYPRINT_AVAILABLE = False


def _snapshot(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "cover": {
            "organization_name": "Acme Foods", "product_name": "Masala Chips",
            "analysis_id": "a1", "generated_at": "2026-01-01T00:00:00Z",
            "report_hash": "deadbeef",
        },
        "verdict_summary": {
            "overall_status": "fail", "confidence_tier": "medium",
            "counts_by_severity": {"major": 1},
        },
        "provenance": {
            "ruleset": {"jurisdiction": "IN", "category": "packaged_food", "version": "1.0.0"},
            "model_manifest": {
                "ocr_engines": ["paddleocr@3.7"], "extractor_provider": "gemini",
                "extractor_model": "gemini-3.8-flash", "normalizer_version": "1.0.0",
                "classifier_version": "1.0.0",
            },
            "file_checksums": [{"filename": "label.jpg", "sha256": "a" * 64}],
        },
        "findings": [
            {
                "rule_key": "IN-TEST-RULE", "rule_version": 1, "severity": "major",
                "status": "fail", "message": "Net quantity is missing units.",
                "citation": "FSSAI reg. 2.2", "reason": None,
                "evidence": [
                    {"text_snippet": "250 g", "image_base64": "aGVsbG8="},
                ],
            }
        ],
        "not_applicable_rules": [],
        "insufficient_data": [],
        "appendix": {
            "extracted_fields": [
                {"field_path": "quantity.net_quantity", "value_raw": "250 g",
                 "confidence": 0.95, "verified": True}
            ],
            "ocr_text_by_page": [{"page_no": 1, "text": "250 g"}],
            "change_log": [],
        },
    }
    base.update(overrides)
    return base


class TestRenderHtml:
    def test_produces_a_well_formed_html_document(self) -> None:
        html = render_html(_snapshot())
        assert html.startswith("<!doctype html>")
        assert "</html>" in html

    def test_includes_cover_and_provenance_content(self) -> None:
        html = render_html(_snapshot())
        assert "Acme Foods" in html
        assert "Masala Chips" in html
        assert "FSSAI reg. 2.2" in html
        assert "gemini" in html

    def test_embeds_the_evidence_crop_as_a_data_uri(self) -> None:
        html = render_html(_snapshot())
        assert "data:image/png;base64,aGVsbG8=" in html

    def test_escapes_label_derived_content_to_prevent_html_injection(self) -> None:
        """The same untrusted-data discipline P3-T5's prompt framing applies
        to LLM input applies here too: a value that looks like it could
        break out of the HTML (as if printed on a label and later
        extracted) must render as inert text, not a live tag/script."""
        snapshot = _snapshot()
        snapshot["cover"]["product_name"] = "<script>alert(1)</script>"
        html = render_html(snapshot)
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html

    def test_an_empty_findings_list_renders_a_none_placeholder(self) -> None:
        html = render_html(_snapshot(findings=[]))
        assert "None." in html


@pytest.mark.skipif(
    not _WEASYPRINT_AVAILABLE,
    reason=(
        "WeasyPrint's native Pango/cairo/gdk-pixbuf libraries are not "
        "importable in this environment (see the `weasyprint` marker)."
    ),
)
@pytest.mark.weasyprint
class TestRenderPdf:
    def test_renders_a_real_pdf_from_the_html_template(self) -> None:
        from app.reports.pdf import render_pdf

        pdf_bytes = render_pdf(render_html(_snapshot()))
        assert pdf_bytes.startswith(b"%PDF-")
