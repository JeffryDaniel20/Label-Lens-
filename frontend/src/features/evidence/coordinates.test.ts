import { describe, expect, it } from "vitest";

import {
  applyViewerTransform,
  bboxToDisplayRect,
  bboxToViewportRect,
  clampScale,
  IDENTITY_TRANSFORM,
  transformToCenterBbox,
} from "@/features/evidence/coordinates";

describe("bboxToDisplayRect", () => {
  it("passes a bbox through unchanged when the image is displayed at its native size", () => {
    const rect = bboxToDisplayRect({ x1: 10, y1: 20, x2: 110, y2: 70 }, 1000, 800, 1000, 800);
    expect(rect).toEqual({ left: 10, top: 20, width: 100, height: 50 });
  });

  it("scales a bbox proportionally when the image is displayed smaller than native", () => {
    // Native 1000x800 shown at half size (500x400) - every axis halves.
    const rect = bboxToDisplayRect({ x1: 100, y1: 200, x2: 300, y2: 400 }, 1000, 800, 500, 400);
    expect(rect).toEqual({ left: 50, top: 100, width: 100, height: 100 });
  });

  it("scales independently per axis when width/height scale differently", () => {
    // Native 1000x800 stretched to 2000x400 (2x width, 0.5x height).
    const rect = bboxToDisplayRect({ x1: 100, y1: 100, x2: 200, y2: 200 }, 1000, 800, 2000, 400);
    expect(rect).toEqual({ left: 200, top: 50, width: 200, height: 50 });
  });

  it("rejects a non-positive native size rather than dividing by zero", () => {
    expect(() => bboxToDisplayRect({ x1: 0, y1: 0, x2: 1, y2: 1 }, 0, 800, 500, 400)).toThrow(
      RangeError,
    );
  });
});

describe("applyViewerTransform", () => {
  it("is a no-op under the identity transform", () => {
    const rect = { left: 10, top: 20, width: 30, height: 40 };
    expect(applyViewerTransform(rect, IDENTITY_TRANSFORM)).toEqual(rect);
  });

  it("scales size and position together", () => {
    const rect = { left: 10, top: 20, width: 30, height: 40 };
    const result = applyViewerTransform(rect, { scale: 2, translateX: 0, translateY: 0 });
    expect(result).toEqual({ left: 20, top: 40, width: 60, height: 80 });
  });

  it("translates after scaling, matching CSS's own transform order", () => {
    const rect = { left: 10, top: 20, width: 30, height: 40 };
    const result = applyViewerTransform(rect, { scale: 2, translateX: 100, translateY: 50 });
    expect(result).toEqual({ left: 120, top: 90, width: 60, height: 80 });
  });
});

describe("bboxToViewportRect (the composed transform a real overlay uses)", () => {
  it("is correctly aligned at 1x zoom with a display-scaled image", () => {
    // Native 2000x1600 page shown at 1000x800 (half), no pan/zoom yet.
    const rect = bboxToViewportRect(
      { x1: 200, y1: 400, x2: 600, y2: 500 },
      2000,
      1600,
      1000,
      800,
      IDENTITY_TRANSFORM,
    );
    expect(rect).toEqual({ left: 100, top: 200, width: 200, height: 50 });
  });

  it("stays correctly aligned after the viewer is panned and zoomed", () => {
    const rect = bboxToViewportRect({ x1: 200, y1: 400, x2: 600, y2: 500 }, 2000, 1600, 1000, 800, {
      scale: 3,
      translateX: 50,
      translateY: -25,
    });
    // Display rect is {left:100, top:200, width:200, height:50} (from the
    // test above) - scale by 3, then translate.
    expect(rect).toEqual({ left: 350, top: 575, width: 600, height: 150 });
  });
});

describe("transformToCenterBbox (zoom-to-evidence)", () => {
  it("centers the bbox in the viewport", () => {
    // A 100x50 (display) bbox at display-origin (0,0) inside a 400x300
    // viewport with generous padding disabled (padding: 0) so scale is
    // driven purely by the fit calculation.
    const transform = transformToCenterBbox(
      { x1: 0, y1: 0, x2: 100, y2: 50 },
      100,
      50,
      100,
      50,
      400,
      300,
      { padding: 0 },
    );
    // scale = min(400/100, 300/50) = min(4, 6) = 4, capped by default maxScale (4) exactly.
    expect(transform.scale).toBeCloseTo(4);
    // bbox center in display space is (50, 25); after scaling by 4 -> (200, 100).
    // translate so that lands on the viewport center (200, 150).
    expect(transform.translateX).toBeCloseTo(200 - 200);
    expect(transform.translateY).toBeCloseTo(150 - 100);

    // Verify the resulting transform genuinely centers the bbox: applying
    // it to the bbox's own display rect must land its center on the
    // viewport's own center.
    const placed = applyViewerTransform({ left: 0, top: 0, width: 100, height: 50 }, transform);
    expect(placed.left + placed.width / 2).toBeCloseTo(200);
    expect(placed.top + placed.height / 2).toBeCloseTo(150);
  });

  it("never exceeds maxScale even for a tiny bbox", () => {
    const transform = transformToCenterBbox(
      { x1: 0, y1: 0, x2: 2, y2: 2 },
      1000,
      1000,
      1000,
      1000,
      800,
      600,
      { maxScale: 5 },
    );
    expect(transform.scale).toBeLessThanOrEqual(5);
  });

  it("never falls below minScale even for a huge bbox spanning the whole page", () => {
    const transform = transformToCenterBbox(
      { x1: 0, y1: 0, x2: 1000, y2: 1000 },
      1000,
      1000,
      1000,
      1000,
      100,
      100,
      { minScale: 0.05 },
    );
    expect(transform.scale).toBeGreaterThanOrEqual(0.05);
  });

  it("respects padding by scaling to a fraction of the viewport, not the full extent", () => {
    const noPadding = transformToCenterBbox(
      { x1: 0, y1: 0, x2: 100, y2: 100 },
      100,
      100,
      100,
      100,
      400,
      400,
      { padding: 0, maxScale: 100 },
    );
    const withPadding = transformToCenterBbox(
      { x1: 0, y1: 0, x2: 100, y2: 100 },
      100,
      100,
      100,
      100,
      400,
      400,
      { padding: 0.25, maxScale: 100 },
    );
    expect(withPadding.scale).toBeLessThan(noPadding.scale);
  });
});

describe("clampScale", () => {
  it("passes an in-range value through unchanged", () => {
    expect(clampScale(2)).toBe(2);
  });

  it("clamps below the minimum", () => {
    expect(clampScale(0.001, 0.1, 8)).toBe(0.1);
  });

  it("clamps above the maximum", () => {
    expect(clampScale(100, 0.1, 8)).toBe(8);
  });
});
