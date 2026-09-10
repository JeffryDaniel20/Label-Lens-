import { useEffect, useMemo, useRef, useState, type PointerEvent, type WheelEvent } from "react";

import type { EvidenceDetailOut } from "@/api/types";
import {
  bboxToViewportRect,
  clampScale,
  IDENTITY_TRANSFORM,
  transformToCenterBbox,
  type ViewerTransform,
} from "@/features/evidence/coordinates";
import { useFindingEvidence } from "@/features/evidence/evidence";
import { useDownloadUrl, useVersionPages, type VersionPage } from "@/features/evidence/pages";

interface LabelViewerProps {
  versionId: string;
  /** The finding currently selected elsewhere on the page (e.g. clicked in
   * the findings list) - `undefined` when nothing is selected. Evidence for
   * it is fetched here, lazily, and drives page-switching + zoom-to-evidence
   * + the highlighted overlay, the literal "click finding -> bbox visible"
   * acceptance criterion. */
  selectedFindingId?: string;
}

function pageLabel(page: VersionPage, multiFile: boolean, fileIndex: number): string {
  return multiFile ? `File ${fileIndex + 1} · Page ${page.page_no}` : `Page ${page.page_no}`;
}

/** One rendered page: fetches its own signed image URL and reports its
 * natural (native raster) size once the image actually loads, since that is
 * the one dimension neither `FilePageOut` nor this component can get wrong
 * independently of the real file - `naturalWidth`/`naturalHeight` come
 * straight from the loaded `<img>`, not recomputed. */
function PageImage({
  page,
  onNaturalSize,
}: {
  page: VersionPage;
  onNaturalSize: (width: number, height: number) => void;
}) {
  const { data: download, isPending, isError } = useDownloadUrl(page.render_key);

  if (isPending) return <p className="p-8 text-sm text-slate-500">Loading page…</p>;
  if (isError || !download) {
    return <p className="p-8 text-sm text-red-700">Could not load this page's image.</p>;
  }

  return (
    <img
      src={download.url}
      alt={`Label page ${page.page_no}`}
      draggable={false}
      className="block max-w-none select-none"
      style={{ width: "100%", height: "auto" }}
      onLoad={(event) => {
        const img = event.currentTarget;
        onNaturalSize(img.naturalWidth || page.width, img.naturalHeight || page.height);
      }}
    />
  );
}

export function LabelViewer({ versionId, selectedFindingId }: LabelViewerProps) {
  const { pages, isPending, isError } = useVersionPages(versionId);
  const { data: evidence } = useFindingEvidence(selectedFindingId);

  const containerRef = useRef<HTMLDivElement>(null);
  const [containerSize, setContainerSize] = useState({ width: 0, height: 0 });
  // An explicit tab click overrides the evidence-driven default below; a
  // manual pan/zoom overrides the evidence-driven auto-fit transform below.
  // Both reset the moment a *different* finding is selected - via React's
  // own documented "adjust state when a prop changes during render"
  // pattern (react.dev), not an effect, so there is never a stale frame.
  const [manualPageId, setManualPageId] = useState<string | undefined>(undefined);
  const [userTransform, setUserTransform] = useState<ViewerTransform | null>(null);
  const [displaySize, setDisplaySize] = useState<{
    pageId: string;
    width: number;
    height: number;
  } | null>(null);
  const dragRef = useRef<{ startX: number; startY: number; origin: ViewerTransform } | null>(null);

  const [prevSelectedFindingId, setPrevSelectedFindingId] = useState(selectedFindingId);
  if (prevSelectedFindingId !== selectedFindingId) {
    setPrevSelectedFindingId(selectedFindingId);
    setManualPageId(undefined);
    setUserTransform(null);
  }

  const fileOrder = useMemo(() => Array.from(new Set(pages.map((p) => p.fileId))), [pages]);
  const multiFile = fileOrder.length > 1;

  // The page to show: an explicit tab click wins; otherwise the selected
  // finding's own evidence page; otherwise the first page. Fully derived -
  // recalculates on its own the instant `evidence` finishes loading.
  const evidencePageId = evidence?.[0]?.file_page_id;
  const activePageId = manualPageId ?? evidencePageId ?? pages[0]?.id;
  const activePage = pages.find((p) => p.id === activePageId);

  const currentDisplaySize =
    displaySize && displaySize.pageId === activePage?.id ? displaySize : null;

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => {
      if (!entry) return;
      const { width, height } = entry.contentRect;
      setContainerSize({ width, height });
    });
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  const evidenceOnActivePage: EvidenceDetailOut[] = useMemo(
    () => (evidence ?? []).filter((e) => e.file_page_id === activePage?.id),
    [evidence, activePage?.id],
  );

  // Zoom-to-evidence, fully derived rather than effect+state: the transform
  // that centers the selected finding's first evidence span, the moment
  // both the evidence data and the target page's display size are known -
  // recalculates automatically as those two async values arrive, with no
  // setState needed to "trigger" it. Falls back to the identity transform
  // (a plain, unzoomed page view) once nothing is selected or the target
  // page hasn't finished loading yet.
  const autoTransform = useMemo((): ViewerTransform => {
    const target = evidenceOnActivePage[0];
    if (!target || !currentDisplaySize || !activePage || containerSize.width === 0) {
      return IDENTITY_TRANSFORM;
    }
    return transformToCenterBbox(
      { x1: target.bbox[0], y1: target.bbox[1], x2: target.bbox[2], y2: target.bbox[3] },
      activePage.width,
      activePage.height,
      currentDisplaySize.width,
      currentDisplaySize.height,
      containerSize.width,
      containerSize.height,
    );
  }, [
    evidenceOnActivePage,
    currentDisplaySize,
    activePage,
    containerSize.width,
    containerSize.height,
  ]);

  // A manual pan/zoom overrides the auto-fit view until the selection
  // changes (reset above) or "Reset view" is pressed.
  const transform = userTransform ?? autoTransform;

  function resetView() {
    setUserTransform(null);
  }

  function zoomBy(factor: number) {
    setUserTransform({ ...transform, scale: clampScale(transform.scale * factor) });
  }

  function handleWheel(event: WheelEvent<HTMLDivElement>) {
    event.preventDefault();
    const factor = event.deltaY < 0 ? 1.1 : 1 / 1.1;
    zoomBy(factor);
  }

  function handlePointerDown(event: PointerEvent<HTMLDivElement>) {
    event.currentTarget.setPointerCapture(event.pointerId);
    dragRef.current = { startX: event.clientX, startY: event.clientY, origin: transform };
  }

  function handlePointerMove(event: PointerEvent<HTMLDivElement>) {
    const drag = dragRef.current;
    if (!drag) return;
    setUserTransform({
      ...drag.origin,
      translateX: drag.origin.translateX + (event.clientX - drag.startX),
      translateY: drag.origin.translateY + (event.clientY - drag.startY),
    });
  }

  function handlePointerUp() {
    dragRef.current = null;
  }

  if (isPending) return <p className="text-sm text-slate-500">Loading label pages…</p>;
  if (isError) return <p className="text-sm text-red-700">Could not load this label's pages.</p>;
  if (pages.length === 0) {
    return <p className="text-sm text-slate-500">No rendered pages are available yet.</p>;
  }

  return (
    <div>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div role="tablist" aria-label="Label pages" className="flex flex-wrap gap-1.5">
          {pages.map((page) => (
            <button
              key={page.id}
              type="button"
              role="tab"
              aria-selected={page.id === activePage?.id}
              onClick={() => setManualPageId(page.id)}
              className={`rounded-md px-2.5 py-1 text-xs font-medium ${
                page.id === activePage?.id
                  ? "bg-slate-900 text-white"
                  : "bg-slate-100 text-slate-700 hover:bg-slate-200"
              }`}
            >
              {pageLabel(page, multiFile, fileOrder.indexOf(page.fileId))}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-1">
          <button
            type="button"
            onClick={() => zoomBy(1 / 1.25)}
            aria-label="Zoom out"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
          >
            −
          </button>
          <button
            type="button"
            onClick={() => zoomBy(1.25)}
            aria-label="Zoom in"
            className="rounded-md border border-slate-300 px-2 py-1 text-sm text-slate-700 hover:bg-slate-100"
          >
            +
          </button>
          <button
            type="button"
            onClick={resetView}
            className="rounded-md border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-100"
          >
            Reset view
          </button>
        </div>
      </div>

      <div
        ref={containerRef}
        onWheel={handleWheel}
        onPointerDown={handlePointerDown}
        onPointerMove={handlePointerMove}
        onPointerUp={handlePointerUp}
        onPointerLeave={handlePointerUp}
        className="relative mt-3 h-[32rem] w-full touch-none overflow-hidden rounded-md border border-slate-200 bg-slate-50"
      >
        {activePage && (
          <div
            className="absolute left-0 top-0 origin-top-left"
            style={{
              width: containerSize.width || undefined,
              transform: `translate(${transform.translateX}px, ${transform.translateY}px) scale(${transform.scale})`,
            }}
          >
            <PageImage
              key={activePage.id}
              page={activePage}
              onNaturalSize={() => {
                const width = containerRef.current?.clientWidth;
                if (!width) return;
                const height = width * (activePage.height / activePage.width);
                setDisplaySize({ pageId: activePage.id, width, height });
              }}
            />
            {currentDisplaySize &&
              evidenceOnActivePage.map((item) => {
                const rect = bboxToViewportRect(
                  { x1: item.bbox[0], y1: item.bbox[1], x2: item.bbox[2], y2: item.bbox[3] },
                  activePage.width,
                  activePage.height,
                  currentDisplaySize.width,
                  currentDisplaySize.height,
                  IDENTITY_TRANSFORM, // the wrapper div above already carries the viewer transform
                );
                return (
                  <div
                    key={item.evidence_span_id}
                    title={item.text_snippet}
                    data-testid="evidence-bbox"
                    className="absolute rounded-sm border-2 border-amber-500 bg-amber-400/20"
                    style={{
                      left: rect.left,
                      top: rect.top,
                      width: rect.width,
                      height: rect.height,
                    }}
                  />
                );
              })}
          </div>
        )}
      </div>
    </div>
  );
}
