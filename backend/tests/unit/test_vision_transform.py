"""Unit tests for the invertible affine coordinate transform primitive."""

from __future__ import annotations

import math

import numpy as np
import pytest

from app.vision.transform import AffineTransform

pytestmark = pytest.mark.unit


class TestIdentity:
    def test_identity_leaves_points_unchanged(self) -> None:
        identity = AffineTransform.identity()
        assert identity.apply((3.5, -2.0)) == pytest.approx((3.5, -2.0))

    def test_identity_is_its_own_inverse(self) -> None:
        identity = AffineTransform.identity()
        inverse = identity.inverse()
        assert inverse.apply((10.0, 20.0)) == pytest.approx((10.0, 20.0))


class TestFromCv2:
    def test_translation_matrix_maps_points_correctly(self) -> None:
        m = np.array([[1.0, 0.0, 5.0], [0.0, 1.0, -3.0]])
        transform = AffineTransform.from_cv2(m)
        assert transform.apply((0.0, 0.0)) == pytest.approx((5.0, -3.0))
        assert transform.apply((1.0, 1.0)) == pytest.approx((6.0, -2.0))

    def test_to_cv2_round_trips(self) -> None:
        m = np.array([[0.5, 0.1, 5.0], [-0.1, 0.5, 2.0]])
        transform = AffineTransform.from_cv2(m)
        assert transform.to_cv2() == pytest.approx(m)


class TestInverse:
    @pytest.mark.parametrize(
        ("angle_degrees", "point"),
        [
            (0.0, (10.0, 20.0)),
            (15.0, (10.0, 20.0)),
            (-30.0, (-5.0, 8.0)),
            (90.0, (100.0, 50.0)),
            (177.0, (3.0, -4.0)),
        ],
    )
    def test_forward_then_inverse_recovers_the_original_point(
        self, angle_degrees: float, point: tuple[float, float]
    ) -> None:
        radians = math.radians(angle_degrees)
        cos, sin = math.cos(radians), math.sin(radians)
        rotation = AffineTransform(
            ((cos, -sin, 4.0), (sin, cos, -7.0), (0.0, 0.0, 1.0))
        )
        forward = rotation.apply(point)
        recovered = rotation.inverse().apply(forward)
        assert recovered == pytest.approx(point, abs=1e-6)


class TestComposition:
    def test_then_composes_in_the_documented_order(self) -> None:
        # self first: translate by (10, 0); other second: translate by (0, 5).
        translate_x = AffineTransform(((1.0, 0.0, 10.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0)))
        translate_y = AffineTransform(((1.0, 0.0, 0.0), (0.0, 1.0, 5.0), (0.0, 0.0, 1.0)))
        composed = translate_x.then(translate_y)
        assert composed.apply((0.0, 0.0)) == pytest.approx((10.0, 5.0))

    def test_composed_transform_round_trips_through_its_own_inverse(self) -> None:
        translate = AffineTransform(((1.0, 0.0, 3.0), (0.0, 1.0, -2.0), (0.0, 0.0, 1.0)))
        scale = AffineTransform(((2.0, 0.0, 0.0), (0.0, 2.0, 0.0), (0.0, 0.0, 1.0)))
        composed = translate.then(scale)
        point = (7.0, -9.0)
        forward = composed.apply(point)
        assert composed.inverse().apply(forward) == pytest.approx(point, abs=1e-9)
