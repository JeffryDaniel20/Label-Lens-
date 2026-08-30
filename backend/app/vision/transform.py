"""Invertible 2D affine coordinate transforms.

Every geometric preprocessing step (rotate, deskew) hands back the exact
transform it applied, mapping a point in its input image to the corresponding
point in its output image. Composing every step's transform in the order they
ran gives one transform from the original page all the way to whatever image
OCR/vision actually looks at - and its inverse is what turns a bounding box
found on that processed image back into a coordinate a human looking at the
original upload can trust. This is the mechanism `app/vision/preprocess.py`
is built around, and the one X-10 ("bounding boxes in original image
coordinates") ultimately depends on.

Represented as a 3x3 homogeneous matrix so composition is plain matrix
multiplication and inversion is a single `numpy.linalg.inv` call, but stored
as a plain nested tuple of floats (not a numpy array) so equality and
`repr()` behave normally on the dataclass.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

Point = tuple[float, float]
_Row = tuple[float, float, float]
Matrix3x3 = tuple[_Row, _Row, _Row]


@dataclass(slots=True, frozen=True)
class AffineTransform:
    matrix: Matrix3x3

    @staticmethod
    def identity() -> AffineTransform:
        return AffineTransform(((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))

    @staticmethod
    def from_cv2(m: np.ndarray) -> AffineTransform:
        """Build from an OpenCV 2x3 affine matrix (as used by `warpAffine`)."""
        a, b, c = (float(v) for v in m[0])
        d, e, f = (float(v) for v in m[1])
        return AffineTransform(((a, b, c), (d, e, f), (0.0, 0.0, 1.0)))

    def to_cv2(self) -> np.ndarray:
        """The equivalent OpenCV 2x3 affine matrix."""
        return np.array(self.matrix[:2], dtype=np.float64)

    def apply(self, point: Point) -> Point:
        x, y = point
        (a, b, c), (d, e, f), _ = self.matrix
        return (a * x + b * y + c, d * x + e * y + f)

    def inverse(self) -> AffineTransform:
        inv = np.linalg.inv(np.array(self.matrix, dtype=np.float64))
        return AffineTransform(tuple(tuple(float(v) for v in row) for row in inv))  # type: ignore[arg-type]

    def then(self, other: AffineTransform) -> AffineTransform:
        """Compose: `self` applied first, then `other`."""
        self_m = np.array(self.matrix, dtype=np.float64)
        other_m = np.array(other.matrix, dtype=np.float64)
        combined = other_m @ self_m
        return AffineTransform(tuple(tuple(float(v) for v in row) for row in combined))  # type: ignore[arg-type]
