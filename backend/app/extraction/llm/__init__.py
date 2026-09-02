"""Provider selection for LLM extraction (P3-T5, D-06)."""

from __future__ import annotations

from app.extraction.llm.base import (
    ExtractionProvider,
    ProviderError,
    ProviderNotConfigured,
    ProviderResponse,
    ProviderUsage,
)
from app.platform.config import Settings

__all__ = [
    "ExtractionProvider",
    "ProviderError",
    "ProviderNotConfigured",
    "ProviderResponse",
    "ProviderUsage",
    "build_provider",
]


def build_provider(settings: Settings) -> ExtractionProvider:
    """Resolve `LABELLENS_LLM_PROVIDER` to a live adapter.

    Raises `ProviderNotConfigured` rather than returning a stub that
    fabricates empty facts - the same posture as `app/ingestion/av.py`,
    where an unreachable AV daemon yields `skipped`, never `clean`.
    """
    if settings.llm_provider == "null":
        raise ProviderNotConfigured(
            "LABELLENS_LLM_PROVIDER is 'null': no extraction provider is configured."
        )
    if not settings.llm_api_key:
        raise ProviderNotConfigured(
            "LABELLENS_LLM_API_KEY is empty: no extraction provider credential is configured."
        )
    if settings.llm_provider == "gemini":
        from app.extraction.llm.gemini import GeminiProvider  # noqa: PLC0415 - lazy SDK import

        return GeminiProvider(
            api_key=settings.llm_api_key,
            timeout_seconds=settings.llm_timeout_seconds,
            max_output_tokens=settings.llm_max_output_tokens,
            temperature=settings.llm_temperature,
        )
    raise ProviderNotConfigured(f"Unknown LLM provider: {settings.llm_provider!r}")
