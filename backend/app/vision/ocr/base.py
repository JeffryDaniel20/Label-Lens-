"""OCR engine adapter interface.

Every OCR backend (PaddleOCR today; a cloud fallback in P3-T3) implements
`OcrEngine` and returns plain `OcrToken`s - text, confidence, an axis-aligned
bounding box, a line grouping, and a detected language - in the coordinate
space of whatever image was actually handed to `run()`. Nothing here maps
those coordinates anywhere: `app/vision/ocr/service.py` is what runs an
engine against a preprocessed page and maps its tokens back to the original
image via the page's `AffineTransform`, matching the "AI extracts, rules
decide" principle - this module only ever emits structured data, never a
verdict.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from app.vision.transform import Point


@dataclass(slots=True, frozen=True)
class OcrToken:
    text: str
    confidence: float
    bbox: tuple[Point, Point]  # (top-left, bottom-right), in the input image's own coordinates
    line_no: int
    language: str | None


class OcrEngine(Protocol):
    """Any adapter capable of running OCR on a single-page image array."""

    name: str
    version: str

    def run(self, image: np.ndarray) -> list[OcrToken]: ...
