/** Pure coordinate math for the label viewer canvas (P6-T4). No DOM, no
 * React - kept this way specifically so it can be unit-tested with plain
 * numbers, matching the task's own "coordinate-transform unit tests" line.
 *
 * A finding's evidence bbox (`EvidenceDetailOut.bbox`) is stored in the
 * *original* rasterized page's pixel space - the exact same space
 * `FilePageOut.width`/`height` describe (backend: `app.vision.preprocess`
 * writes both from the same raster). The `<img>` element the browser
 * actually renders that page into is very rarely displayed at that native
 * pixel size (it's laid out to fit the viewer's own container), so a bbox
 * can never be drawn by copying its raw numbers onto the page - it must
 * first be rescaled from native pixels to *display* pixels, then have the
 * viewer's own pan/zoom transform applied on top. Both steps are captured
 * here as their own pure functions so each is independently testable. */

export interface Bbox {
  x1: number;
  y1: number;
  x2: number;
  y2: number;
}

export interface ViewerTransform {
  /** Multiplies both axes uniformly - the canvas never distorts a page's
   * aspect ratio, matching how a physical label is never stretched. */
  scale: number;
  translateX: number;
  translateY: number;
}

export interface ViewportRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

export const IDENTITY_TRANSFORM: ViewerTransform = { scale: 1, translateX: 0, translateY: 0 };

/** Rescale a bbox from the page's native raster pixel space into the
 * `<img>` element's actual rendered (CSS) pixel space - the two coincide
 * only when the image happens to be displayed at 1:1, which the viewer
 * never assumes. */
export function bboxToDisplayRect(
  bbox: Bbox,
  nativeWidth: number,
  nativeHeight: number,
  displayWidth: number,
  displayHeight: number,
): ViewportRect {
  if (nativeWidth <= 0 || nativeHeight <= 0) {
    throw new RangeError("nativeWidth/nativeHeight must be positive");
  }
  const scaleX = displayWidth / nativeWidth;
  const scaleY = displayHeight / nativeHeight;
  return {
    left: bbox.x1 * scaleX,
    top: bbox.y1 * scaleY,
    width: (bbox.x2 - bbox.x1) * scaleX,
    height: (bbox.y2 - bbox.y1) * scaleY,
  };
}

/** Apply the viewer's own pan/zoom on top of an already display-scaled
 * rect, producing the final on-screen position relative to the canvas's
 * untransformed top-left corner. */
export function applyViewerTransform(rect: ViewportRect, transform: ViewerTransform): ViewportRect {
  return {
    left: rect.left * transform.scale + transform.translateX,
    top: rect.top * transform.scale + transform.translateY,
    width: rect.width * transform.scale,
    height: rect.height * transform.scale,
  };
}

/** The one-step composition of both functions above - what a bbox overlay
 * component actually calls every render. */
export function bboxToViewportRect(
  bbox: Bbox,
  nativeWidth: number,
  nativeHeight: number,
  displayWidth: number,
  displayHeight: number,
  transform: ViewerTransform,
): ViewportRect {
  return applyViewerTransform(
    bboxToDisplayRect(bbox, nativeWidth, nativeHeight, displayWidth, displayHeight),
    transform,
  );
}

/** "Zoom-to-evidence": the transform that centers `bbox` in a
 * `viewportWidth` x `viewportHeight` container with `padding` (a fraction of
 * the viewport, e.g. 0.15 = 15% breathing room on each side) around it,
 * without ever exceeding `maxScale` - a bbox that is only a few pixels tall
 * (a single short word) should not zoom in to an absurd, disorienting
 * magnification. This is the pure math behind the acceptance criterion
 * "selecting any finding brings its evidence into view in one click,
 * correctly aligned at any zoom" - the alignment is `bboxToViewportRect`
 * above, the "brings into view" is this. */
export function transformToCenterBbox(
  bbox: Bbox,
  nativeWidth: number,
  nativeHeight: number,
  displayWidth: number,
  displayHeight: number,
  viewportWidth: number,
  viewportHeight: number,
  options: { padding?: number; maxScale?: number; minScale?: number } = {},
): ViewerTransform {
  const padding = options.padding ?? 0.15;
  const maxScale = options.maxScale ?? 4;
  const minScale = options.minScale ?? 0.1;

  const rect = bboxToDisplayRect(bbox, nativeWidth, nativeHeight, displayWidth, displayHeight);
  const targetWidth = viewportWidth * (1 - padding * 2);
  const targetHeight = viewportHeight * (1 - padding * 2);

  const rawScale =
    rect.width <= 0 || rect.height <= 0
      ? 1
      : Math.min(targetWidth / rect.width, targetHeight / rect.height);
  const scale = Math.min(maxScale, Math.max(minScale, rawScale));

  const bboxCenterX = rect.left + rect.width / 2;
  const bboxCenterY = rect.top + rect.height / 2;

  return {
    scale,
    translateX: viewportWidth / 2 - bboxCenterX * scale,
    translateY: viewportHeight / 2 - bboxCenterY * scale,
  };
}

/** Clamp a zoom/pan interaction's resulting scale into a sane range - used
 * by the canvas's own wheel-zoom handler, kept here so the bound is a
 * single, tested source of truth rather than re-typed at each call site. */
export function clampScale(scale: number, minScale = 0.1, maxScale = 8): number {
  return Math.min(maxScale, Math.max(minScale, scale));
}
