"""Section 23's "Image quality" and "Coverage" families.

Image quality - *degrade to lower confidence tier and route to review,
never silently pass.*
Coverage - *`insufficient_data`, not `pass`.*

Both are asserted through the real pipeline, at the layer where the
behaviour actually lives: a degraded photograph's real consequence is
low-confidence OCR tokens, and the routing decision from those is
`app.confidence.tiers` + `_scoring`. One `paddleocr`-marked test runs a
genuinely degraded *image* through real OCR to prove the premise that
degradation really does lower OCR confidence, rather than assuming it.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw, ImageFilter

from tests.security.adversarial.rig import (
    adversarial_case,
    baseline_extraction,
    not_found,
    run_adversarial,
)

pytestmark = [pytest.mark.security, pytest.mark.integration]


class TestImageQualityRoutesToReview:
    @pytest.mark.parametrize(
        ("case_id", "token_confidence"),
        [
            ("blurred", 0.55),
            ("low_contrast", 0.62),
            ("glare", 0.48),
            ("downscaled_50pct", 0.66),
            ("rotated_5deg", 0.69),
        ],
    )
    def test_a_degraded_read_never_silently_passes(
        self, db, case_id: str, token_confidence: float
    ) -> None:
        """Every one of these confidences is below the 0.70 MEDIUM
        threshold, so the analysis must land in a review state - never
        `completed` - even though every field was extracted and every rule
        would otherwise pass."""
        result = run_adversarial(
            db,
            adversarial_case(
                case_id=f"image-{case_id}", token_confidence=token_confidence
            ),
        )

        assert result.confidence_tier == "low"
        # The rules still evaluate (the data is all there); the point is
        # that the analysis is not auto-completed on an unreliable read.
        assert result.actual_findings
        assert all(status != "fail" for status in result.actual_findings.values())

    def test_a_borderline_read_degrades_to_medium_not_high(self, db) -> None:
        result = run_adversarial(
            db, adversarial_case(case_id="image-borderline", token_confidence=0.80)
        )
        assert result.confidence_tier == "medium"

    def test_a_clean_read_is_the_control_and_is_not_degraded(self, db) -> None:
        """The control that makes the assertions above meaningful: the same
        label read cleanly does NOT route to review, so the degraded cases
        are demonstrating a real effect, not a pipeline that always
        reviews."""
        result = run_adversarial(
            db, adversarial_case(case_id="image-clean-control", token_confidence=0.99)
        )
        assert result.confidence_tier == "high"


@pytest.mark.paddleocr
class TestDegradationReallyLowersRealOcrConfidence:
    """The premise behind the whole family, verified against real OCR
    rather than assumed: a degraded image genuinely reads worse than the
    same image clean. Marked `paddleocr` because it needs the real engine
    (present in this project's Docker image)."""

    @staticmethod
    def _label_image() -> Image.Image:
        image = Image.new("RGB", (600, 200), color=(255, 255, 255))
        draw = ImageDraw.Draw(image)
        draw.text((20, 80), "NET QUANTITY 250 g", fill=(0, 0, 0))
        return image

    def test_a_blurred_label_reads_worse_than_the_same_label_clean(self) -> None:
        import numpy as np

        from app.vision.ocr.paddle import PaddleOcrEngine

        engine = PaddleOcrEngine()
        clean = self._label_image()
        blurred = clean.filter(ImageFilter.GaussianBlur(radius=4))

        def _confidence(image: Image.Image) -> float:
            tokens = engine.run(np.array(image))
            if not tokens:
                return 0.0  # the blur destroyed the text entirely
            return sum(t.confidence for t in tokens) / len(tokens)

        clean_confidence = _confidence(clean)
        blurred_confidence = _confidence(blurred)

        # Either the blur lowers confidence, or it destroys the text
        # entirely (0.0) - both are "degrades", neither is "reads the same".
        assert clean_confidence > 0.0, "the clean control must read at all"
        assert blurred_confidence < clean_confidence


class TestCoverageIsInsufficientDataNotPass:
    @pytest.mark.parametrize(
        ("case_id", "envelope_key", "rule_key"),
        [
            (
                "missing-nutrition-panel",
                "nutrition_serving_size",
                "IN-FSSAI-FOOD-NUTRITION-SERVING-SIZE-DECLARED",
            ),
            (
                "cropped-ingredient-list",
                "ingredients_declared_text",
                "IN-FSSAI-FOOD-INGREDIENTS-LIST-DECLARED",
            ),
            (
                "obscured-net-weight",
                "quantity_net_quantity",
                "IN-FSSAI-FOOD-NET-QUANTITY-DECLARED",
            ),
            (
                "half-visible-expiry",
                "dates_expiry_or_best_before",
                "IN-FSSAI-FOOD-EXPIRY-DATE-DECLARED",
            ),
        ],
    )
    def test_an_unreadable_panel_is_insufficient_data_never_pass(
        self, db, case_id: str, envelope_key: str, rule_key: str
    ) -> None:
        extraction = baseline_extraction()
        extraction[envelope_key] = not_found("panel not visible in the photograph")

        result = run_adversarial(
            db, adversarial_case(case_id=f"coverage-{case_id}", extraction=extraction)
        )

        assert result.actual_findings[rule_key] == "insufficient_data"
        assert result.actual_findings[rule_key] != "pass"
        assert result.confidence_tier != "high"

    def test_a_half_visible_allergen_line_does_not_become_a_passing_allergen_check(
        self, db
    ) -> None:
        extraction = baseline_extraction()
        extraction["allergens_declared"] = {
            "values": [],
            "not_found_reason": "allergen line is cut off at the edge of the photograph",
            "token_ids": [],
            "confidence": 0.0,
        }
        extraction["allergens_declaration_text"] = not_found("cut off at the packet seam")

        result = run_adversarial(
            db, adversarial_case(case_id="coverage-allergen", extraction=extraction)
        )

        assert (
            result.actual_findings["IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED"]
            == "insufficient_data"
        )
