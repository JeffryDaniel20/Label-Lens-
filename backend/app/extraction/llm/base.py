"""LLM provider adapter interface for structured extraction (P3-T5).

Mirrors `app/vision/ocr/base.py`: a vendor sits behind a narrow `Protocol`
so switching providers is configuration (`LABELLENS_LLM_PROVIDER`), not an
architectural change - the posture D-06 records, and the same one that keeps
`app/storage/client.py` vendor-neutral for D-02.

A provider's single job is to send a prompt and hand back **raw text plus
usage**. It deliberately does *not* parse, validate, repair, or retry:
schema validation, the repair retry, and model escalation are
provider-independent policy and live in `app/extraction/service.py`, so
every provider gets identical anti-hallucination behaviour rather than each
adapter reimplementing it slightly differently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pydantic import BaseModel


class ProviderError(Exception):
    """The provider call itself failed (transport, auth, rate limit, refusal)."""


class ProviderNotConfigured(ProviderError):
    """No usable credential/provider is configured.

    Raised - never swallowed into an empty-but-valid fact set. An analysis
    with no extraction provider must fail explicitly, exactly like a file
    with no AV daemon is marked `skipped` rather than silently `clean`.
    """


@dataclass(slots=True, frozen=True)
class ProviderUsage:
    """What the call actually cost, as reported by the provider itself -
    fed straight into `app.analysis.costs.record_stage_cost` (P5-T5)."""

    tokens_in: int
    tokens_out: int


@dataclass(slots=True, frozen=True)
class ProviderResponse:
    text: str
    usage: ProviderUsage
    # The model string the provider says actually served the request, which
    # is not always the one requested (aliases, silent version pinning) -
    # this is what belongs in the model manifest, not the requested name.
    model: str


class ExtractionProvider(Protocol):
    """Any adapter capable of a single constrained-JSON generation call."""

    name: str

    def generate(
        self,
        *,
        system_instruction: str,
        prompt: str,
        schema: type[BaseModel],
        model: str,
    ) -> ProviderResponse: ...
