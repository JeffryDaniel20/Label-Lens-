"""Image preprocessing with tracked coordinate transforms.

Five steps - deskew, rotate, denoise, enhance contrast, binarize - each take
an image and hand back `(new_image, AffineTransform)`. The two geometric
steps (`rotate`, and `deskew` which calls it) return the transform they
actually applied; the three pixel-only steps return the identity transform,
since nothing moves. `preprocess()` runs the full pipeline and composes every
step's transform into one `to_original`, so a bounding box found anywhere on
the fully preprocessed image can be mapped straight back to a coordinate on
the original page.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from app.vision.transform import AffineTransform, Point

# Below this, a detected skew is noise (JPEG artifacts, a handful of stray
# pixels) rather than a real scan skew, and rotating would just blur the
# image for no benefit.
_MIN_SKEW_DEGREES = 0.1
# A page with fewer foreground pixels than this is treated as blank -
# there's nothing to estimate a skew angle from.
_MIN_FOREGROUND_PIXELS = 20


@dataclass(slots=True, frozen=True)
class PreprocessedPage:
    image: np.ndarray
    to_original: AffineTransform


def to_grayscale(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def rotate(image: np.ndarray, angle_degrees: float) -> tuple[np.ndarray, AffineTransform]:
    """Rotate by `angle_degrees` (counter-clockwise, positive), expanding the
    canvas so no content is cropped."""
    height, width = image.shape[:2]
    center = (width / 2.0, height / 2.0)
    matrix = cv2.getRotationMatrix2D(center, angle_degrees, 1.0)
    cos = abs(matrix[0, 0])
    sin = abs(matrix[0, 1])
    new_width = int(height * sin + width * cos)
    new_height = int(height * cos + width * sin)
    matrix[0, 2] += (new_width / 2.0) - center[0]
    matrix[1, 2] += (new_height / 2.0) - center[1]
    border_value = (255, 255, 255) if image.ndim == 3 else 255
    rotated = cv2.warpAffine(
        image,
        matrix,
        (new_width, new_height),
        borderValue=border_value,
        flags=cv2.INTER_CUBIC,
    )
    return rotated, AffineTransform.from_cv2(matrix)


def _estimate_skew_degrees(gray: np.ndarray) -> float:
    _, foreground = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    ys, xs = np.where(foreground > 0)
    if len(xs) < _MIN_FOREGROUND_PIXELS:
        return 0.0
    points = np.column_stack([xs, ys]).astype(np.float32)
    _, _, raw_angle = cv2.minAreaRect(points)
    # minAreaRect's angle is only defined modulo 90 (a rectangle looks the
    # same rotated by a multiple of 90 degrees with sides swapped), so fold
    # it into (-45, 45] before treating it as a correction angle.
    angle = raw_angle % 90.0
    if angle > 45.0:
        angle -= 90.0
    return angle


def deskew(image: np.ndarray) -> tuple[np.ndarray, AffineTransform]:
    """Detect and correct small-angle skew (e.g. from an imperfect scan)."""
    angle = _estimate_skew_degrees(image)
    if abs(angle) < _MIN_SKEW_DEGREES:
        return image, AffineTransform.identity()
    return rotate(image, angle)


def denoise(image: np.ndarray) -> tuple[np.ndarray, AffineTransform]:
    denoised = cv2.fastNlMeansDenoising(image, None, 10, 7, 21)
    return denoised, AffineTransform.identity()


def enhance_contrast(image: np.ndarray) -> tuple[np.ndarray, AffineTransform]:
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    return clahe.apply(image), AffineTransform.identity()


def binarize(image: np.ndarray) -> tuple[np.ndarray, AffineTransform]:
    _, binary = cv2.threshold(image, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    return binary, AffineTransform.identity()


def preprocess(image: np.ndarray) -> PreprocessedPage:
    gray = to_grayscale(image)
    combined = AffineTransform.identity()
    current = gray
    for step in (deskew, denoise, enhance_contrast, binarize):
        current, transform = step(current)
        combined = combined.then(transform)
    return PreprocessedPage(image=current, to_original=combined.inverse())


def map_bbox_to_original(
    bbox: tuple[Point, Point], to_original: AffineTransform
) -> tuple[Point, Point]:
    """Map an axis-aligned (top-left, bottom-right) bbox on the preprocessed
    image back to the original page's coordinates.

    `to_original` may include a rotation (from `deskew`), which turns an
    axis-aligned rectangle into a general quadrilateral - mapping only the
    top-left and bottom-right corners would silently drop two corners of
    that quadrilateral and can even flip min/max ordering. All four corners
    are mapped and the axis-aligned bounding box of the result is returned,
    which is what a rotation-tolerant bbox must do to stay meaningful.
    """
    (x1, y1), (x2, y2) = bbox
    corners = [
        to_original.apply((x1, y1)),
        to_original.apply((x2, y1)),
        to_original.apply((x2, y2)),
        to_original.apply((x1, y2)),
    ]
    xs = [c[0] for c in corners]
    ys = [c[1] for c in corners]
    return (min(xs), min(ys)), (max(xs), max(ys))
