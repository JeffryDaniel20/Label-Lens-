"""Unit tests for image preprocessing and its coordinate transforms.

The central property under test (per IMPLEMENTATION.md P3-T1's acceptance
criterion) is that any bounding box produced on a preprocessed image can be
expressed in original-image coordinates: a known point, mapped forward by a
step's transform and back by its inverse, must land within 1px of where it
started - on both a plainly rotated fixture and a deliberately skewed one.
This is a claim about the transform bookkeeping being self-consistent, not
about how accurately `deskew()` estimates a skew angle; a separate, looser
test covers that the estimate is at least in the right ballpark.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from app.vision.preprocess import (
    binarize,
    denoise,
    deskew,
    enhance_contrast,
    map_bbox_to_original,
    preprocess,
    rotate,
    to_grayscale,
)

pytestmark = pytest.mark.unit


def _lined_page(*, size: int = 300) -> np.ndarray:
    """A synthetic "page" with several horizontal bars, evocative of text
    lines - enough structure for `minAreaRect`-based skew estimation to find
    a dominant orientation."""
    image = np.full((size, size), 255, dtype=np.uint8)
    for y in range(90, 210, 18):
        cv2.rectangle(image, (60, y), (240, y + 8), 0, -1)
    return image


class TestRotateRoundTrip:
    @pytest.mark.parametrize("angle_degrees", [0.0, 7.0, -12.0, 30.0, -44.0, 90.0])
    def test_a_known_point_maps_forward_and_back_within_one_pixel(
        self, angle_degrees: float
    ) -> None:
        image = _lined_page()
        known_point = (150.0, 150.0)  # the page's center, a point on a bar

        rotated, transform = rotate(image, angle_degrees)
        forward = transform.apply(known_point)
        # The forward-mapped point must actually land inside the rotated canvas.
        assert 0 <= forward[0] < rotated.shape[1]
        assert 0 <= forward[1] < rotated.shape[0]

        recovered = transform.inverse().apply(forward)
        assert recovered == pytest.approx(known_point, abs=1.0)


class TestDeskewRoundTrip:
    @pytest.mark.parametrize("true_skew_degrees", [3.0, -8.0, 15.0, -22.0])
    def test_a_known_point_survives_a_full_pipeline_round_trip_on_a_skewed_fixture(
        self, true_skew_degrees: float
    ) -> None:
        page = _lined_page()
        skewed, _ = rotate(page, true_skew_degrees)
        known_point = (skewed.shape[1] / 2.0, skewed.shape[0] / 2.0)

        result = preprocess(skewed)

        # Map the known point through every step's forward transform by
        # replaying the same pipeline the production code runs, then check
        # the pipeline's own composed `to_original` inverts it correctly.
        _, deskew_transform = deskew(to_grayscale(skewed))
        forward_point = deskew_transform.apply(known_point)
        recovered = result.to_original.apply(forward_point)
        assert recovered == pytest.approx(known_point, abs=1.0)

    def test_deskew_estimates_roughly_the_correct_correction_angle(self) -> None:
        # A loose sanity check that this isn't a no-op stub - not a claim
        # about precise angle-detection accuracy.
        page = to_grayscale(_lined_page())
        for true_skew_degrees in (10.0, -15.0):
            skewed, _ = rotate(page, true_skew_degrees)
            _, transform = deskew(skewed)
            # Recover the applied rotation's angle from its matrix (cv2's
            # convention: matrix[0] == (cos(theta), sin(theta), tx)).
            a, b = transform.matrix[0][0], transform.matrix[0][1]
            applied_degrees = np.degrees(np.arctan2(b, a))
            expected_correction = -true_skew_degrees
            assert applied_degrees == pytest.approx(expected_correction, abs=3.0)

    def test_a_blank_page_is_left_untouched(self) -> None:
        blank = np.full((100, 100), 255, dtype=np.uint8)
        result, transform = deskew(blank)
        assert transform.apply((5.0, 5.0)) == pytest.approx((5.0, 5.0))
        assert result.shape == blank.shape


class TestPixelOnlySteps:
    def test_denoise_returns_the_identity_transform_and_same_shape(self) -> None:
        image = to_grayscale(_lined_page())
        denoised, transform = denoise(image)
        assert denoised.shape == image.shape
        assert transform.apply((1.0, 2.0)) == pytest.approx((1.0, 2.0))

    def test_enhance_contrast_returns_the_identity_transform_and_same_shape(self) -> None:
        image = to_grayscale(_lined_page())
        enhanced, transform = enhance_contrast(image)
        assert enhanced.shape == image.shape
        assert transform.apply((1.0, 2.0)) == pytest.approx((1.0, 2.0))

    def test_binarize_returns_the_identity_transform_and_a_strictly_binary_image(
        self,
    ) -> None:
        image = to_grayscale(_lined_page())
        binary, transform = binarize(image)
        assert binary.shape == image.shape
        assert set(np.unique(binary).tolist()) <= {0, 255}
        assert transform.apply((1.0, 2.0)) == pytest.approx((1.0, 2.0))


class TestFullPipeline:
    def test_preprocess_produces_a_binary_image_and_a_working_inverse_transform(
        self,
    ) -> None:
        page = _lined_page()
        result = preprocess(page)
        assert set(np.unique(result.image).tolist()) <= {0, 255}

        center_of_processed = (result.image.shape[1] / 2.0, result.image.shape[0] / 2.0)
        original_point = result.to_original.apply(center_of_processed)
        # Round-tripping back through the forward direction should recover
        # the same point on the processed image.
        forward_again = result.to_original.inverse().apply(original_point)
        assert forward_again == pytest.approx(center_of_processed, abs=1.0)

    def test_map_bbox_to_original_matches_direct_corner_mapping_when_axis_aligned(
        self,
    ) -> None:
        page = _lined_page()
        result = preprocess(page)
        bbox = ((10.0, 10.0), (50.0, 40.0))
        top_left, bottom_right = map_bbox_to_original(bbox, result.to_original)
        # `_lined_page()` is already axis-aligned, so `to_original` here is
        # (near) identity/translation-only - mapping just the two corners
        # directly should agree with the general 4-corner bbox mapping.
        assert top_left == pytest.approx(result.to_original.apply(bbox[0]), abs=1.0)
        assert bottom_right == pytest.approx(result.to_original.apply(bbox[1]), abs=1.0)

    def test_map_bbox_to_original_stays_axis_aligned_under_rotation(self) -> None:
        # A `to_original` that includes a real rotation turns an axis-aligned
        # bbox into a quadrilateral; mapping all 4 corners must still yield a
        # valid axis-aligned box that contains every one of them.
        _, rotate_transform = rotate(_lined_page(), 25.0)
        to_original = rotate_transform.inverse()
        bbox = ((10.0, 10.0), (50.0, 40.0))

        top_left, bottom_right = map_bbox_to_original(bbox, to_original)
        assert top_left[0] <= bottom_right[0]
        assert top_left[1] <= bottom_right[1]

        (x1, y1), (x2, y2) = bbox
        corners = [
            to_original.apply((x1, y1)),
            to_original.apply((x2, y1)),
            to_original.apply((x2, y2)),
            to_original.apply((x1, y2)),
        ]
        for cx, cy in corners:
            assert top_left[0] - 1e-6 <= cx <= bottom_right[0] + 1e-6
            assert top_left[1] - 1e-6 <= cy <= bottom_right[1] + 1e-6
