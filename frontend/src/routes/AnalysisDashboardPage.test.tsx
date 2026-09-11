import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AnalysisDashboardPage } from "@/routes/AnalysisDashboardPage";
import { ToastProvider } from "@/lib/toast";

const {
  useProductMock,
  useProductVersionMock,
  useAnalysisMock,
  useAnalysisEventsMock,
  useFindingsMock,
  useReportsMock,
  useGenerateReportMock,
  useAnalysisSseMock,
  useSessionMock,
  useAnalysisSignoffMock,
  useSignOffAnalysisMock,
  useDecideFindingMock,
  useCorrectFieldMock,
} = vi.hoisted(() => ({
  useProductMock: vi.fn(),
  useProductVersionMock: vi.fn(),
  useAnalysisMock: vi.fn(),
  useAnalysisEventsMock: vi.fn(),
  useFindingsMock: vi.fn(),
  useReportsMock: vi.fn(),
  useGenerateReportMock: vi.fn(),
  useAnalysisSseMock: vi.fn(),
  useSessionMock: vi.fn(),
  useAnalysisSignoffMock: vi.fn(),
  useSignOffAnalysisMock: vi.fn(),
  useDecideFindingMock: vi.fn(),
  useCorrectFieldMock: vi.fn(),
}));
vi.mock("@/features/catalog/products", () => ({ useProduct: useProductMock }));
vi.mock("@/features/catalog/versions", () => ({ useProductVersion: useProductVersionMock }));
vi.mock("@/features/analysis/analysis", () => ({
  useAnalysis: useAnalysisMock,
  useAnalysisEvents: useAnalysisEventsMock,
  useFindings: useFindingsMock,
  useReports: useReportsMock,
  useGenerateReport: useGenerateReportMock,
}));
vi.mock("@/features/analysis/useAnalysisSse", () => ({ useAnalysisSse: useAnalysisSseMock }));
vi.mock("@/features/review/review", () => ({
  useAnalysisSignoff: useAnalysisSignoffMock,
  useSignOffAnalysis: useSignOffAnalysisMock,
  useDecideFinding: useDecideFindingMock,
  useCorrectField: useCorrectFieldMock,
}));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));
// The label viewer canvas (P6-T4) is its own dedicated suite
// (`LabelViewer.test.tsx`) - stubbed here to a plain marker so this file
// stays focused on dashboard-level behavior, and to prove the dashboard
// wires the *selected finding* through correctly (the one thing that
// crosses the boundary between the two).
vi.mock("@/components/LabelViewer", () => ({
  LabelViewer: ({
    versionId,
    selectedFindingId,
  }: {
    versionId: string;
    selectedFindingId?: string;
  }) => (
    <div data-testid="label-viewer">
      viewer for {versionId}, selected: {selectedFindingId ?? "none"}
    </div>
  ),
}));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/products/p1/versions/v1/analyses/a1"]}>
          <Routes>
            <Route
              path="/products/:productId/versions/:versionId/analyses/:analysisId"
              element={<AnalysisDashboardPage />}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const RUNNING_ANALYSIS = {
  id: "a1",
  product_version_id: "v1",
  state: "ocr" as const,
  confidence_tier: null,
  failure_stage: null,
  retryable: null,
  started_at: "2026-01-01T00:00:00Z",
  finished_at: null,
  progress_percentage: 33,
  total_tokens_in: 0,
  total_tokens_out: 0,
  total_cost_cents: 0,
};

describe("AnalysisDashboardPage", () => {
  beforeEach(() => {
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: { id: "v1", version_no: 1 } });
    useAnalysisEventsMock.mockReturnValue({ data: [] });
    useFindingsMock.mockReturnValue({ data: [] });
    useReportsMock.mockReturnValue({ data: [] });
    useGenerateReportMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useAnalysisSseMock.mockReturnValue({ connected: true });
    useSessionMock.mockReturnValue({
      data: { capabilities: ["analysis:view", "report:generate"] },
    });
    useAnalysisSignoffMock.mockReturnValue({ data: null });
    useSignOffAnalysisMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useDecideFindingMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useCorrectFieldMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
  });

  it("shows the current state, progress, and a live indicator while running", () => {
    useAnalysisMock.mockReturnValue({ data: RUNNING_ANALYSIS, isPending: false, error: null });

    renderPage();

    expect(screen.getByText("Reading label (OCR)")).toBeInTheDocument();
    expect(screen.getByText("33% complete")).toBeInTheDocument();
    expect(screen.getByText("Live")).toBeInTheDocument();
  });

  it("hides the live indicator once the analysis has stopped", () => {
    useAnalysisMock.mockReturnValue({
      data: {
        ...RUNNING_ANALYSIS,
        state: "completed",
        progress_percentage: 100,
        confidence_tier: "high",
      },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText("Completed")).toBeInTheDocument();
    expect(screen.getByText("high confidence")).toBeInTheDocument();
    expect(screen.queryByText("Live")).not.toBeInTheDocument();
  });

  it("shows the failure stage and the last event's reason for a failed analysis", () => {
    useAnalysisMock.mockReturnValue({
      data: {
        ...RUNNING_ANALYSIS,
        state: "failed",
        failure_stage: "extracting",
        retryable: true,
        progress_percentage: 44,
      },
      isPending: false,
      error: null,
    });
    useAnalysisEventsMock.mockReturnValue({
      data: [
        {
          sequence: 1,
          from_state: null,
          to_state: "queued",
          occurred_at: "2026-01-01T00:00:00Z",
          reason: null,
        },
        {
          sequence: 2,
          from_state: "extracting",
          to_state: "failed",
          occurred_at: "2026-01-01T00:01:00Z",
          reason: "Gemini call failed: 503 UNAVAILABLE.",
        },
      ],
    });

    renderPage();

    const banner = screen.getByText(/Analysis failed at "extracting"/).closest("div")!;
    expect(within(banner).getByText("Gemini call failed: 503 UNAVAILABLE.")).toBeInTheDocument();
    expect(within(banner).getByText(/retried automatically/)).toBeInTheDocument();
    expect(screen.getAllByText("Gemini call failed: 503 UNAVAILABLE.")).toHaveLength(2);
  });

  it("renders the real (possibly empty) findings list without fabricating anything", () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useFindingsMock.mockReturnValue({ data: [] });

    renderPage();

    expect(
      screen.getByText("No compliance findings are available for this analysis."),
    ).toBeInTheDocument();
  });

  it("renders real findings when present", () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useFindingsMock.mockReturnValue({
      data: [
        {
          id: "f1",
          analysis_id: "a1",
          rule_key: "IN.FSSAI.NET_QUANTITY",
          rule_version: 1,
          status: "fail",
          severity: "major",
          message: "Net quantity not declared.",
          details: {},
          confidence: 0.9,
          evidence_refs: [],
        },
      ],
    });

    renderPage();

    expect(screen.getByText("IN.FSSAI.NET_QUANTITY")).toBeInTheDocument();
    expect(screen.getByText("major")).toBeInTheDocument();
    // "fail" also appears as a status-filter button label now (P6-T5) - the
    // status badge on the finding itself is the second match.
    expect(screen.getAllByText("fail")).toHaveLength(2);
  });

  it("shows a sign-off button for a reviewer on an analysis awaiting review", () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useSessionMock.mockReturnValue({
      data: { capabilities: ["analysis:view", "report:generate", "analysis:signoff"] },
    });

    renderPage();

    expect(screen.getByRole("button", { name: "Sign off review" })).toBeInTheDocument();
  });

  it("hides the sign-off button from a viewer without analysis:signoff", () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.queryByRole("button", { name: "Sign off review" })).not.toBeInTheDocument();
    expect(screen.getByText("Awaiting reviewer sign-off.")).toBeInTheDocument();
  });

  it("shows a read-only badge and hides the sign-off button once signed off", () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useSessionMock.mockReturnValue({
      data: { capabilities: ["analysis:view", "report:generate", "analysis:signoff"] },
    });
    useAnalysisSignoffMock.mockReturnValue({
      data: {
        id: "s1",
        analysis_id: "a1",
        ruleset_version_id: null,
        finding_set_hash: "abc",
        signed_off_at: "2026-01-02T00:00:00Z",
        actor_label: "reviewer@example.com",
      },
    });

    renderPage();

    expect(screen.getByText("Signed off · read-only")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Sign off review" })).not.toBeInTheDocument();
  });

  it("calls the sign-off mutation when the button is clicked", async () => {
    const mutate = vi.fn();
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useSessionMock.mockReturnValue({
      data: { capabilities: ["analysis:view", "report:generate", "analysis:signoff"] },
    });
    useSignOffAnalysisMock.mockReturnValue({ mutate, isPending: false });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Sign off review" }));

    expect(mutate).toHaveBeenCalledWith(
      undefined,
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });

  it("selects a finding with evidence and passes it to the label viewer", async () => {
    useAnalysisMock.mockReturnValue({
      data: { ...RUNNING_ANALYSIS, state: "needs_review", confidence_tier: "low" },
      isPending: false,
      error: null,
    });
    useFindingsMock.mockReturnValue({
      data: [
        {
          id: "f1",
          analysis_id: "a1",
          rule_key: "IN-FSSAI-FOOD-NET-QUANTITY-DECLARED",
          rule_version: 1,
          status: "pass",
          severity: "critical",
          message: "Net quantity is declared.",
          details: {},
          confidence: 0.95,
          evidence_refs: [{ extracted_field_id: "ef1", evidence_span_id: "es1" }],
        },
        {
          id: "f2",
          analysis_id: "a1",
          rule_key: "IN-FSSAI-FOOD-INGREDIENTS-ITEMS-ITEMIZED",
          rule_version: 1,
          status: "insufficient_data",
          severity: "minor",
          message: null,
          details: {},
          confidence: 0,
          evidence_refs: [],
        },
      ],
    });

    renderPage();
    const user = userEvent.setup();

    expect(screen.getByTestId("label-viewer")).toHaveTextContent("selected: none");

    // A finding with no evidence (`f2`) cannot be selected - the button is
    // disabled, matching P5-T4's own contract that insufficient_data/
    // not_applicable findings carry no evidence rows.
    expect(
      screen.getByRole("button", { name: /IN-FSSAI-FOOD-INGREDIENTS-ITEMS-ITEMIZED/ }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: /IN-FSSAI-FOOD-NET-QUANTITY-DECLARED/ }));

    expect(screen.getByTestId("label-viewer")).toHaveTextContent("selected: f1");
  });

  it("generates an extraction report and shows the resulting field table", async () => {
    const mutate = vi.fn();
    useGenerateReportMock.mockReturnValue({ mutate, isPending: false });
    useAnalysisMock.mockReturnValue({
      data: {
        ...RUNNING_ANALYSIS,
        state: "completed",
        confidence_tier: "high",
        progress_percentage: 100,
      },
      isPending: false,
      error: null,
    });
    useReportsMock.mockReturnValue({
      data: [
        {
          id: "r1",
          analysis_id: "a1",
          kind: "json",
          generated_at: "2026-01-01T00:05:00Z",
          sha256: "abc",
          pdf_key: null,
          pdf_download_url: null,
          pdf_download_expires_in: null,
          snapshot: {
            appendix: {
              extracted_fields: [
                {
                  field_path: "quantity.net_quantity",
                  value_raw: "250 g",
                  value_norm: { value: 250, unit: "g" },
                  confidence: 0.95,
                  verified: true,
                },
              ],
            },
          },
        },
      ],
    });

    renderPage();
    const user = userEvent.setup();

    expect(screen.getByText("quantity.net_quantity")).toBeInTheDocument();
    expect(screen.getByText("95%")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Regenerate extraction report" }));
    expect(mutate).toHaveBeenCalledTimes(1);
  });
});
