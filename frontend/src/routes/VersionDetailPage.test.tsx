import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VersionDetailPage } from "@/routes/VersionDetailPage";
import { ToastProvider } from "@/lib/toast";

const {
  useProductMock,
  useProductVersionMock,
  useVersionFilesMock,
  useFileUploadsMock,
  useSessionMock,
  useSubmitAnalysisMock,
} = vi.hoisted(() => ({
  useProductMock: vi.fn(),
  useProductVersionMock: vi.fn(),
  useVersionFilesMock: vi.fn(),
  useFileUploadsMock: vi.fn(),
  useSessionMock: vi.fn(),
  useSubmitAnalysisMock: vi.fn(),
}));
vi.mock("@/features/catalog/products", () => ({ useProduct: useProductMock }));
vi.mock("@/features/catalog/versions", () => ({ useProductVersion: useProductVersionMock }));
vi.mock("@/features/catalog/uploads", () => ({
  useVersionFiles: useVersionFilesMock,
  useFileUploads: useFileUploadsMock,
}));
vi.mock("@/features/analysis/analysis", () => ({ useSubmitAnalysis: useSubmitAnalysisMock }));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/products/p1/versions/v1"]}>
          <Routes>
            <Route
              path="/products/:productId/versions/:versionId"
              element={<VersionDetailPage />}
            />
            <Route
              path="/products/:productId/versions/:versionId/analyses/:analysisId"
              element={<div>Analysis Dashboard</div>}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const DEFAULT_VERSION = {
  id: "v1",
  product_id: "p1",
  version_no: 1,
  label: "",
  status: "draft" as const,
  locked_at: null,
  superseded_at: null,
  created_at: "2026-01-01T00:00:00Z",
};

describe("VersionDetailPage", () => {
  beforeEach(() => {
    useSubmitAnalysisMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
  });

  it("shows the upload dropzone and existing files for a user who can upload", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "file:upload"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({
      data: [
        {
          id: "f1",
          product_version_id: "v1",
          original_filename: "front.jpg",
          mime: "image/jpeg",
          bytes: 204_800,
          page_count: null,
          status: "ready",
          av_status: "clean",
          rejection_reason: null,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();

    expect(screen.getByLabelText("Upload label files")).toBeInTheDocument();
    expect(screen.getByText("front.jpg")).toBeInTheDocument();
    expect(screen.getByText(/image\/jpeg/)).toBeInTheDocument();
  });

  it("hides the dropzone for a viewer without file:upload", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();

    expect(screen.queryByLabelText("Upload label files")).not.toBeInTheDocument();
  });

  it("hides the dropzone once the version is locked, even for an uploader", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "file:upload"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({
      data: { ...DEFAULT_VERSION, status: "locked", locked_at: "2026-01-02T00:00:00Z" },
      isPending: false,
      error: null,
    });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();

    expect(screen.queryByLabelText("Upload label files")).not.toBeInTheDocument();
    expect(screen.getByText(/locked because an analysis depends on it/)).toBeInTheDocument();
  });

  it("shows per-file upload progress", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "file:upload"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({
      tasks: [{ id: "u1", filename: "front.jpg", progress: 42, status: "uploading" }],
      uploadFiles: vi.fn(),
      dismissTask: vi.fn(),
    });

    renderPage();

    expect(screen.getByText("42%")).toBeInTheDocument();
  });

  it("invokes uploadFiles when a file is selected via the browse input", async () => {
    const uploadFiles = vi.fn();
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "file:upload"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles, dismissTask: vi.fn() });

    renderPage();
    const user = userEvent.setup();
    const file = new File(["hello"], "front.jpg", { type: "image/jpeg" });
    const input = screen.getByLabelText("Upload label files").querySelector("input")!;
    await user.upload(input, file);

    expect(uploadFiles).toHaveBeenCalledTimes(1);
    const [firstCall] = uploadFiles.mock.calls;
    const passedFiles = firstCall?.[0] as FileList;
    expect(passedFiles[0]?.name).toBe("front.jpg");
  });

  it("hides the analysis section for a user without analysis:run", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "file:upload"] } });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();

    expect(screen.queryByRole("button", { name: "Run analysis" })).not.toBeInTheDocument();
  });

  it("disables Run analysis until at least one file is ready", () => {
    useSessionMock.mockReturnValue({
      data: { capabilities: ["product:view", "file:upload", "analysis:run"] },
    });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({ data: [] });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();

    expect(screen.getByRole("button", { name: "Run analysis" })).toBeDisabled();
  });

  it("submits an analysis and navigates to its dashboard on success", async () => {
    const mutate = vi.fn((_payload, options: { onSuccess: (a: { id: string }) => void }) => {
      options.onSuccess({ id: "a1" });
    });
    useSubmitAnalysisMock.mockReturnValue({ mutate, isPending: false });
    useSessionMock.mockReturnValue({
      data: { capabilities: ["product:view", "file:upload", "analysis:run"] },
    });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: DEFAULT_VERSION, isPending: false, error: null });
    useVersionFilesMock.mockReturnValue({
      data: [
        {
          id: "f1",
          product_version_id: "v1",
          original_filename: "front.jpg",
          mime: "image/jpeg",
          bytes: 1024,
          page_count: null,
          status: "ready",
          av_status: "clean",
          rejection_reason: null,
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    useFileUploadsMock.mockReturnValue({ tasks: [], uploadFiles: vi.fn(), dismissTask: vi.fn() });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Run analysis" }));

    expect(mutate).toHaveBeenCalledTimes(1);
    expect(await screen.findByText("Analysis Dashboard")).toBeInTheDocument();
  });
});
