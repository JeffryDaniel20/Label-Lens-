"""Google Gemini adapter (D-06, P3-T5).

`google-genai` is imported lazily inside `__init__`, matching
`app/vision/ocr/paddle.py`: importing this module must stay free for
mypy/ruff and for every test that never makes a provider call.

Gemini's native `response_schema` accepts a Pydantic model directly and
constrains decoding to it, which is why this provider is a good fit for the
"strict JSON schema as the output contract" requirement in IMPLEMENTATION.md
§8 step 5 - the contract is enforced during generation, not only checked
afterwards. The afterwards-check still happens in `service.py` regardless,
because a schema-shaped response can still be semantically wrong.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from app.extraction.llm.base import ProviderError, ProviderResponse, ProviderUsage


class GeminiProvider:
    name = "gemini"

    def __init__(
        self,
        *,
        api_key: str,
        timeout_seconds: int = 90,
        max_output_tokens: int = 8192,
        temperature: float = 0.0,
    ) -> None:
        from google import genai  # noqa: PLC0415 - lazy on purpose, see docstring
        from google.genai import types

        self._types = types
        self._client = genai.Client(
            api_key=api_key,
            http_options=types.HttpOptions(timeout=timeout_seconds * 1000),
        )
        self._max_output_tokens = max_output_tokens
        self._temperature = temperature

    def generate(
        self,
        *,
        system_instruction: str,
        prompt: str,
        schema: type[BaseModel],
        model: str,
    ) -> ProviderResponse:
        types = self._types
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=schema,
            temperature=self._temperature,
            max_output_tokens=self._max_output_tokens,
            # No tools, deliberately: with a strict schema and no tool
            # surface there is no channel an injected instruction could act
            # through even if the model were persuaded by one.
            tools=[],
        )
        try:
            response = self._client.models.generate_content(
                model=model, contents=prompt, config=config
            )
        except Exception as exc:  # provider SDKs raise a wide, unstable set
            raise ProviderError(f"Gemini call failed: {exc}") from exc

        text = response.text
        if not text:
            # A blocked or empty candidate is a real outcome (safety filter,
            # token exhaustion) and must surface as a failure rather than as
            # an empty-but-valid fact set.
            raise ProviderError(
                "Gemini returned no text "
                f"(finish reason: {_finish_reason(response)!r})."
            )

        usage = response.usage_metadata
        return ProviderResponse(
            text=text,
            usage=ProviderUsage(
                tokens_in=int(getattr(usage, "prompt_token_count", 0) or 0),
                tokens_out=int(getattr(usage, "candidates_token_count", 0) or 0),
            ),
            model=response.model_version or model,
        )


def _finish_reason(response: Any) -> str | None:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return None
    reason = getattr(candidates[0], "finish_reason", None)
    return str(reason) if reason is not None else None
