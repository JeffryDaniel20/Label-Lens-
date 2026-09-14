"""Real Google Cloud Vision verification (P3-T3, D-03).

Skipped unless `LABELLENS_OCR_FALLBACK_GOOGLE_VISION_API_KEY` is set, the
same opt-in pattern as `test_extraction_live.py`'s real-Gemini tests and
`postgres`/`object_storage`/`clamav`/`paddleocr`/`redis`. This is the one
test that proves the adapter genuinely talks to Google Cloud Vision and gets
back real, usable OCR - everything else in the P3-T3 suite proves the
escalation policy and the response-parsing logic against a fixture.

Costs a small amount of real quota and is therefore never part of the
default suite.
"""

from __future__ import annotations

import os

import numpy as np
import pytest
from PIL import Image, ImageDraw

from app.vision.ocr.google_vision import GoogleVisionOcrEngine

pytestmark = [pytest.mark.integration, pytest.mark.google_vision]

API_KEY = os.environ.get("LABELLENS_OCR_FALLBACK_GOOGLE_VISION_API_KEY")

pytestmark.append(
    pytest.mark.skipif(
        not API_KEY, reason="LABELLENS_OCR_FALLBACK_GOOGLE_VISION_API_KEY is not set"
    )
)


def _label_image() -> np.ndarray:
    image = Image.new("RGB", (500, 150), color=(255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.text((20, 20), "NET QUANTITY", fill=(0, 0, 0))
    draw.text((20, 70), "250 g", fill=(0, 0, 0))
    return np.array(image)


class TestLiveGoogleVision:
    def test_a_real_call_reads_the_printed_text(self) -> None:
        engine = GoogleVisionOcrEngine(api_key=API_KEY or "", timeout_seconds=30)

        tokens = engine.run(_label_image())

        assert tokens, "Google Cloud Vision returned no tokens for a clean synthetic label"
        joined = " ".join(t.text for t in tokens).upper()
        assert "NET QUANTITY" in joined
        assert "250" in joined
        assert all(0.0 <= t.confidence <= 1.0 for t in tokens)
        assert all(t.bbox[0][0] <= t.bbox[1][0] and t.bbox[0][1] <= t.bbox[1][1] for t in tokens)

    def test_engine_identity_matches_what_ocr_results_will_record(self) -> None:
        engine = GoogleVisionOcrEngine(api_key=API_KEY or "")
        assert engine.name == "google_vision"
        assert engine.version
