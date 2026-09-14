"""Google Cloud Vision adapter (P3-T3, D-03 resolved 2026-09-14).

Google Cloud Vision was chosen over Azure Read to close D-03 because
IMPLEMENTATION.md section 4's own architecture table already named it as
this project's OCR fallback ("Google Cloud Vision *document_text_detection*
... escalate only when Paddle confidence is low; bounded per-tenant
budget"), and it needs no second cloud account: this codebase already talks
to Google for LLM extraction (D-06, `app/extraction/llm/gemini.py`), so one
more Google API is one credential family to manage, not two.

Reached over the plain REST `images:annotate` endpoint with an API key
(`httpx`, already a core dependency - no new SDK), the same "one HTTP call,
one JSON response" shape the `google-genai` SDK itself uses under the hood,
and simpler than pulling in `google-cloud-vision` and its service-account
JSON auth for a single feature. `DOCUMENT_TEXT_DETECTION` (not plain
`TEXT_DETECTION`) is the feature requested specifically because it is the
only one of the two that reports a real per-word `confidence` - `OcrToken`
requires one, and `TEXT_DETECTION`'s flat `textAnnotations` array does not
carry it at all.

**Granularity, stated plainly**: each `OcrToken` here is one Vision
*paragraph*, not one word or one Paddle-style detected line - Vision's
`fullTextAnnotation` groups words into paragraphs in reading order already,
and a label panel's paragraphs are usually one visual line or a short
tightly-wrapped run, which is the same rough granularity
`PaddleOcrEngine` already produces (see its own module docstring). A
paragraph's `confidence` is the mean of its words' own confidences; its
`bbox` is the axis-aligned box around its `boundingBox` vertices; its
`language` is its own `detectedLanguages[0]`, falling back to the page's.
"""

from __future__ import annotations

import base64
import importlib.metadata
from typing import Any

import cv2
import httpx
import numpy as np

from app.vision.ocr.base import OcrToken

_ANNOTATE_URL = "https://vision.googleapis.com/v1/images:annotate"


class VisionApiError(Exception):
    """The Vision API call itself failed (transport, auth, quota, or a
    per-image error the API reports inside an otherwise-200 response)."""


class GoogleVisionOcrEngine:
    """`OcrEngine` backed by Google Cloud Vision's `DOCUMENT_TEXT_DETECTION`."""

    name = "google_vision"

    def __init__(self, *, api_key: str, timeout_seconds: int = 30) -> None:
        if not api_key:
            raise ValueError("GoogleVisionOcrEngine requires a non-empty api_key.")
        self._api_key = api_key
        self._timeout_seconds = timeout_seconds
        try:
            self.version = importlib.metadata.version("httpx")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unknown"

    def run(self, image: np.ndarray) -> list[OcrToken]:
        ok, buf = cv2.imencode(".png", image)
        if not ok:
            raise VisionApiError("Could not encode the image for the Vision API request.")
        content_b64 = base64.b64encode(buf.tobytes()).decode("ascii")

        body = {
            "requests": [
                {
                    "image": {"content": content_b64},
                    "features": [{"type": "DOCUMENT_TEXT_DETECTION"}],
                }
            ]
        }
        try:
            response = httpx.post(
                _ANNOTATE_URL,
                params={"key": self._api_key},
                json=body,
                timeout=self._timeout_seconds,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise VisionApiError(f"Google Cloud Vision request failed: {exc}") from exc

        result = (payload.get("responses") or [{}])[0]
        if "error" in result:
            raise VisionApiError(f"Google Cloud Vision returned an error: {result['error']}")

        return _tokens_from_full_text_annotation(result.get("fullTextAnnotation") or {})


def _tokens_from_full_text_annotation(annotation: dict[str, Any]) -> list[OcrToken]:
    tokens: list[OcrToken] = []
    line_no = 0
    for page in annotation.get("pages", []):
        page_language = _detected_language(page.get("property"))
        for block in page.get("blocks", []):
            for paragraph in block.get("paragraphs", []):
                token = _token_from_paragraph(paragraph, line_no, fallback_language=page_language)
                if token is not None:
                    tokens.append(token)
                    line_no += 1
    return tokens


def _token_from_paragraph(
    paragraph: dict[str, Any], line_no: int, *, fallback_language: str | None
) -> OcrToken | None:
    words = paragraph.get("words", [])
    if not words:
        return None

    text = " ".join(_word_text(word) for word in words).strip()
    if not text:
        return None

    confidences = [float(w["confidence"]) for w in words if "confidence" in w]
    confidence = sum(confidences) / len(confidences) if confidences else 0.0

    bbox = _axis_aligned_bbox(paragraph.get("boundingBox"))
    if bbox is None:
        return None

    language = _detected_language(paragraph.get("property")) or fallback_language

    return OcrToken(
        text=text, confidence=confidence, bbox=bbox, line_no=line_no, language=language
    )


def _word_text(word: dict[str, Any]) -> str:
    return "".join(symbol.get("text", "") for symbol in word.get("symbols", []))


def _axis_aligned_bbox(
    bounding_box: dict[str, Any] | None,
) -> tuple[tuple[float, float], tuple[float, float]] | None:
    if not bounding_box:
        return None
    vertices = bounding_box.get("vertices") or []
    points = [(float(v.get("x", 0)), float(v.get("y", 0))) for v in vertices]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys)), (max(xs), max(ys))


def _detected_language(prop: dict[str, Any] | None) -> str | None:
    if not prop:
        return None
    languages = prop.get("detectedLanguages") or []
    if not languages:
        return None
    code = languages[0].get("languageCode")
    return str(code) if code else None
