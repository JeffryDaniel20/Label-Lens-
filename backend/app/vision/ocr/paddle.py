"""PaddleOCR adapter.

PaddleOCR's default pipeline detects and recognizes whole text *lines* rather
than individual words - word-level boxes need `return_word_box=True`, which
this adapter deliberately leaves off to keep the dependency's own internal
preprocessing (and thus its accuracy) at its well-tested default. So each
`OcrToken` here is one recognized line: `line_no` is that line's position in
PaddleOCR's own detection order, and `bbox` is the axis-aligned box PaddleOCR
itself reports (`rec_boxes`), not the polygon it also returns (`rec_polys`) -
plenty precise for the panels label text actually needs to be located in.
`language` is the engine's configured language, since this pipeline
(`use_textline_orientation=False`) does not report per-line language
detection.
"""

from __future__ import annotations

import importlib.metadata
import os

import cv2
import numpy as np

from app.vision.ocr.base import OcrToken

# PaddlePaddle's PIR execution mode combined with oneDNN on CPU raises
# `NotImplementedError: ConvertPirAttribute2RuntimeAttribute ...` on at least
# one real (paddlepaddle 3.3.1 CPU wheel) build encountered verifying this
# adapter - a paddle-side bug, not anything this code does. Disabling oneDNN
# avoids the broken code path entirely; must be set before the first
# predictor is built, so it's set at import time.
os.environ.setdefault("FLAGS_use_mkldnn", "0")


class PaddleOcrEngine:
    """`OcrEngine` backed by a local PaddleOCR pipeline (PP-OCRv6)."""

    name = "paddleocr"

    def __init__(self, *, lang: str = "en") -> None:
        from paddleocr import PaddleOCR

        self._lang = lang
        self._engine = PaddleOCR(
            lang=lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            device="cpu",
            enable_mkldnn=False,
        )
        try:
            self.version = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            self.version = "unknown"

    def run(self, image: np.ndarray) -> list[OcrToken]:
        # PaddleOCR requires a 3-channel image; `preprocess()` hands this
        # adapter a single-channel binarized page.
        bgr = image if image.ndim == 3 else cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)

        tokens: list[OcrToken] = []
        for page_result in self._engine.predict(bgr):
            texts = page_result["rec_texts"]
            scores = page_result["rec_scores"]
            boxes = page_result["rec_boxes"]
            for line_no, (text, score, box) in enumerate(zip(texts, scores, boxes, strict=True)):
                x1, y1, x2, y2 = (float(v) for v in box)
                tokens.append(
                    OcrToken(
                        text=text,
                        confidence=float(score),
                        bbox=((x1, y1), (x2, y2)),
                        line_no=line_no,
                        language=self._lang,
                    )
                )
        return tokens
