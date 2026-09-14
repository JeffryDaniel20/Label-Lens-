"""Fallback OCR engine selection (P3-T3, D-03 resolved 2026-09-14: Google Cloud Vision).

Mirrors `app/extraction/llm/__init__.py::build_provider`'s own shape: a
vendor is resolved from `LABELLENS_OCR_FALLBACK_PROVIDER` behind the same
narrow `OcrEngine` Protocol the primary `PaddleOcrEngine` already
implements, so adding a *second* cloud vendor later is a config change plus
one new adapter class, not a pipeline change.
"""

from __future__ import annotations

from app.platform.config import Settings
from app.vision.ocr.base import OcrEngine

__all__ = ["build_ocr_fallback_engine"]


def build_ocr_fallback_engine(settings: Settings) -> OcrEngine | None:
    """`None` means escalation is disabled - `run_ocr_with_escalation`
    treats that exactly like an exhausted budget: keep the primary result,
    never fail the analysis over a missing fallback.

    `"google_vision"` selected without a real API key also degrades to
    `None` rather than raising: an operator who flips the config on before
    setting the credential should get "escalation quietly stays off," not a
    pipeline-wide crash on the next analysis - the same posture
    `app/extraction/llm/__init__.py::build_provider` uses for a missing LLM
    key, except that path raises because extraction has no fallback of its
    own to degrade to, while OCR escalation's whole point is optional.
    """
    if settings.ocr_fallback_provider == "null":
        return None
    if settings.ocr_fallback_provider == "google_vision":
        if not settings.ocr_fallback_google_vision_api_key:
            return None
        from app.vision.ocr.google_vision import GoogleVisionOcrEngine

        return GoogleVisionOcrEngine(
            api_key=settings.ocr_fallback_google_vision_api_key,
            timeout_seconds=settings.ocr_fallback_timeout_seconds,
        )
    raise ValueError(f"Unknown OCR fallback provider: {settings.ocr_fallback_provider!r}")
