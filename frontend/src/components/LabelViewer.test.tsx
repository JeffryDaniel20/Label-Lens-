import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { LabelViewer } from "@/components/LabelViewer";

const { useVersionPagesMock, useDownloadUrlMock, useFindingEvidenceMock } = vi.hoisted(() => ({
  useVersionPagesMock: vi.fn(),
  useDownloadUrlMock: vi.fn(),
  useFindingEvidenceMock: vi.fn(),
}));
vi.mock("@/features/evidence/pages", () => ({
  useVersionPages: useVersionPagesMock,
  useDownloadUrl: useDownloadUrlMock,
}));
vi.mock("@/features/evidence/evidence", () => ({ useFindingEvidence: useFindingEvidenceMock }));

// jsdom never actually decodes a fake `src`, so `naturalWidth`/`naturalHeight`
// on a fired `load` event are always 0 - `LabelViewer`'s own fallback to the
// page's own `width`/`height` (see `PageImage`) is exactly what makes this
// safe to rely on in a test environment too, not a workaround invented here.
function loadActiveImage() {
  fireEvent.load(screen.getByRole("img"));
}

const PAGE_1 = {
  id: "page1",
  fileId: "file1",
  page_no: 1,
  width: 1000,
  height: 1400,
  render_key: "k1",
};
const PAGE_2 = {
  id: "page2",
  fileId: "file1",
  page_no: 2,
  width: 1000,
  height: 1400,
  render_key: "k2",
};

function renderViewer(selectedFindingId?: string) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <LabelViewer versionId="v1" selectedFindingId={selectedFindingId} />
    </QueryClientProvider>,
  );
}

describe("LabelViewer", () => {
  beforeEach(() => {
    useVersionPagesMock.mockReturnValue({
      pages: [PAGE_1, PAGE_2],
      isPending: false,
      isError: false,
    });
    useDownloadUrlMock.mockReturnValue({
      data: { url: "https://cdn.example/page.png", expires_in: 300 },
      isPending: false,
      isError: false,
    });
    useFindingEvidenceMock.mockReturnValue({ data: undefined });

    // ResizeObserver isn't implemented in jsdom.
    // eslint-disable-next-line @typescript-eslint/no-explicit-any
    (globalThis as any).ResizeObserver = class {
      observe() {}
      disconnect() {}
    };
    // jsdom never performs real layout, so `clientWidth` is always 0 -
    // stubbed to a plausible viewport width so the display-size (and
    // therefore bbox-overlay) calculation has something real to work with,
    // the same reasoning `naturalWidth`/`naturalHeight` fall back to the
    // page's own dimensions above.
    Object.defineProperty(HTMLElement.prototype, "clientWidth", {
      configurable: true,
      value: 800,
    });
  });

  it("renders a tab per page and defaults to the first one", () => {
    renderViewer();

    const tabs = screen.getAllByRole("tab");
    expect(tabs.map((tab) => tab.textContent)).toEqual(["Page 1", "Page 2"]);
    expect(screen.getByRole("tab", { name: "Page 1" })).toHaveAttribute("aria-selected", "true");
  });

  it("switches pages when a different tab is clicked", async () => {
    renderViewer();
    const user = userEvent.setup();

    await user.click(screen.getByRole("tab", { name: "Page 2" }));

    expect(screen.getByRole("tab", { name: "Page 2" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: "Page 1" })).toHaveAttribute("aria-selected", "false");
  });

  it("shows nothing yet with no selected finding", () => {
    renderViewer();
    loadActiveImage();

    expect(screen.queryByTitle(/./)).not.toBeInTheDocument();
  });

  it("selecting a finding switches to its evidence's page and renders the bbox", () => {
    useFindingEvidenceMock.mockReturnValue({
      data: [
        {
          extracted_field_id: "ef1",
          field_path: "quantity.net_quantity",
          evidence_span_id: "es1",
          file_page_id: "page2",
          page_no: 2,
          bbox: [100, 200, 300, 250],
          text_snippet: "250 g",
          source: "ocr",
          page_image_url: "https://cdn.example/page2.png",
          page_image_expires_in: 300,
        },
      ],
    });

    renderViewer("f1");

    // The click-to-evidence page switch is derived state - no need to
    // simulate the image loading before it takes effect.
    expect(screen.getByRole("tab", { name: "Page 2" })).toHaveAttribute("aria-selected", "true");

    // The overlay itself only appears once the page's own display size is
    // known (the real `<img>` has genuinely loaded).
    expect(screen.queryByTitle("250 g")).not.toBeInTheDocument();
    loadActiveImage();
    expect(screen.getByTitle("250 g")).toBeInTheDocument();
  });

  it("only overlays evidence belonging to the currently active page", () => {
    useFindingEvidenceMock.mockReturnValue({
      data: [
        {
          extracted_field_id: "ef1",
          field_path: "quantity.net_quantity",
          evidence_span_id: "es1",
          file_page_id: "page1",
          page_no: 1,
          bbox: [10, 10, 50, 30],
          text_snippet: "Page one evidence",
          source: "ocr",
          page_image_url: "u",
          page_image_expires_in: 300,
        },
        {
          extracted_field_id: "ef2",
          field_path: "dates.batch_number",
          evidence_span_id: "es2",
          file_page_id: "page2",
          page_no: 2,
          bbox: [10, 10, 50, 30],
          text_snippet: "Page two evidence",
          source: "ocr",
          page_image_url: "u",
          page_image_expires_in: 300,
        },
      ],
    });

    // No `selectedFindingId` fed in via props this time - land on the
    // default first page and confirm only that page's own evidence shows.
    renderViewer();
    loadActiveImage();

    expect(screen.getByTitle("Page one evidence")).toBeInTheDocument();
    expect(screen.queryByTitle("Page two evidence")).not.toBeInTheDocument();
  });

  it("shows a loading state while pages are being fetched", () => {
    useVersionPagesMock.mockReturnValue({ pages: [], isPending: true, isError: false });
    renderViewer();
    expect(screen.getByText("Loading label pages…")).toBeInTheDocument();
  });

  it("shows an honest empty state when a version genuinely has no pages yet", () => {
    useVersionPagesMock.mockReturnValue({ pages: [], isPending: false, isError: false });
    renderViewer();
    expect(screen.getByText("No rendered pages are available yet.")).toBeInTheDocument();
  });
});
