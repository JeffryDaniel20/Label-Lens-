"""Fallback OCR engine selection (P3-T3, D-03 - vendor still open).

Mirrors `app/extraction/llm/__init__.py::build_provider`'s own shape: a
vendor is resolved from `LABELLENS_OCR_FALLBACK_PROVIDER` behind the same
narrow `OcrEngine` Protocol the primary `PaddleOcrEngine` already
implements, so adding a real cloud vendor later is a config change plus one
new adapter class, not a pipeline change. Only `"null"` (disabled) exists
today - see `app/vision/ocr/escalation.py`'s own module docstring for why.
"""

from __future__ import annotations

from app.platform.config import Settings
from app.vision.ocr.base import OcrEngine

__all__ = ["build_ocr_fallback_engine"]


def build_ocr_fallback_engine(settings: Settings) -> OcrEngine | None:
    """`None` means escalation is disabled - `run_ocr_with_escalation`
    treats that exactly like an exhausted budget: keep the primary result,
    never fail the analysis over a missing fallback."""
    if settings.ocr_fallback_provider == "null":
        return None
    raise ValueError(f"Unknown OCR fallback provider: {settings.ocr_fallback_provider!r}")
