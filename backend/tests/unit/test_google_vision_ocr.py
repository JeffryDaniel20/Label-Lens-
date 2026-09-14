"""Google Cloud Vision adapter (P3-T3, D-03 resolved 2026-09-14).

Contract tests against a recorded-shape fixture (`fixtures/google_vision_
document_text_detection.json`, a hand-built but schema-accurate
`DOCUMENT_TEXT_DETECTION` response) - no real network call and no real
credential needed to prove the adapter parses a genuine Vision API response
shape correctly. `httpx.post` is monkeypatched at the point this module
calls it; nothing about `GoogleVisionOcrEngine.run`'s own HTTP-call code path
is skipped. The one test that actually talks to Google lives in
`tests/integration/test_ocr_google_vision_live.py`, skipped unless
`LABELLENS_OCR_FALLBACK_GOOGLE_VISION_API_KEY` is set, the same opt-in
pattern as `test_extraction_live.py`'s real-Gemini tests.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from app.vision.ocr.google_vision import GoogleVisionOcrEngine, VisionApiError

pytestmark = pytest.mark.unit

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "google_vision_document_text_detection.json"


def _load_fixture() -> dict[str, Any]:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


class _FakeResponse:
    def __init__(self, payload: dict[str, Any], *, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            import httpx

            raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    def json(self) -> dict[str, Any]:
        return self._payload


@pytest.fixture
def image() -> np.ndarray:
    return np.zeros((50, 100, 3), dtype=np.uint8)


class TestConstruction:
    def test_an_empty_api_key_is_rejected_immediately(self) -> None:
        with pytest.raises(ValueError, match="non-empty api_key"):
            GoogleVisionOcrEngine(api_key="")


class TestParsingARecordedResponse:
    def test_two_paragraphs_become_two_tokens_with_real_confidence_and_bbox(
        self, monkeypatch: pytest.MonkeyPatch, image: np.ndarray
    ) -> None:
        monkeypatch.setattr(
            "app.vision.ocr.google_vision.httpx.post",
            lambda *a, **kw: _FakeResponse(_load_fixture()),
        )
        engine = GoogleVisionOcrEngine(api_key="test-key")

        tokens = engine.run(image)

        assert [t.text for t in tokens] == ["NET QUANTITY", "250 g"]
        assert tokens[0].confidence == pytest.approx((0.98 + 0.96) / 2)
        assert tokens[0].bbox == ((10.0, 10.0), (200.0, 40.0))
        assert tokens[0].language == "en"
        assert tokens[1].confidence == pytest.approx((0.94 + 0.90) / 2)
        assert tokens[1].bbox == ((10.0, 60.0), (90.0, 90.0))
        # The second paragraph carries no per-word language in the fixture -
        # it must fall back to the page's own detected language, not None.
        assert tokens[1].language == "en"
        assert [t.line_no for t in tokens] == [0, 1]

    def test_engine_identity(self) -> None:
        engine = GoogleVisionOcrEngine(api_key="test-key")
        assert engine.name == "google_vision"
        assert engine.version

    def test_an_empty_annotation_yields_no_tokens(
        self, monkeypatch: pytest.MonkeyPatch, image: np.ndarray
    ) -> None:
        monkeypatch.setattr(
            "app.vision.ocr.google_vision.httpx.post",
            lambda *a, **kw: _FakeResponse({"responses": [{"fullTextAnnotation": {}}]}),
        )
        engine = GoogleVisionOcrEngine(api_key="test-key")
        assert engine.run(image) == []


class TestErrorHandling:
    def test_a_per_image_api_error_raises(
        self, monkeypatch: pytest.MonkeyPatch, image: np.ndarray
    ) -> None:
        monkeypatch.setattr(
            "app.vision.ocr.google_vision.httpx.post",
            lambda *a, **kw: _FakeResponse(
                {"responses": [{"error": {"code": 3, "message": "Bad image data."}}]}
            ),
        )
        engine = GoogleVisionOcrEngine(api_key="test-key")
        with pytest.raises(VisionApiError, match="Bad image data"):
            engine.run(image)

    def test_an_http_transport_error_raises_vision_api_error(
        self, monkeypatch: pytest.MonkeyPatch, image: np.ndarray
    ) -> None:
        monkeypatch.setattr(
            "app.vision.ocr.google_vision.httpx.post",
            lambda *a, **kw: _FakeResponse({}, status_code=403),
        )
        engine = GoogleVisionOcrEngine(api_key="bad-key")
        with pytest.raises(VisionApiError, match="request failed"):
            engine.run(image)
