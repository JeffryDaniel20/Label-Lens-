import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ReviewQueuePage } from "@/routes/ReviewQueuePage";
import { ToastProvider } from "@/lib/toast";

const {
  useReviewQueueMock,
  useAssignReviewerMock,
  useSessionMock,
  useProductMock,
  useProductVersionMock,
} = vi.hoisted(() => ({
  useReviewQueueMock: vi.fn(),
  useAssignReviewerMock: vi.fn(),
  useSessionMock: vi.fn(),
  useProductMock: vi.fn(),
  useProductVersionMock: vi.fn(),
}));
vi.mock("@/features/review/review", () => ({
  useReviewQueue: useReviewQueueMock,
  useAssignReviewer: useAssignReviewerMock,
}));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));
vi.mock("@/features/catalog/products", () => ({ useProduct: useProductMock }));
vi.mock("@/features/catalog/versions", () => ({ useProductVersion: useProductVersionMock }));

const ENTRY = {
  analysis: {
    id: "a1",
    product_version_id: "v1",
    state: "needs_review" as const,
    confidence_tier: "low" as const,
    failure_stage: null,
    retryable: null,
    started_at: "2026-01-01T00:00:00Z",
    finished_at: null,
    progress_percentage: 100,
    total_tokens_in: 0,
    total_tokens_out: 0,
    total_cost_cents: 0,
    assigned_reviewer_id: null,
  },
  sla_since: new Date(Date.now() - 90 * 60_000).toISOString(),
};

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/review/queue"]}>
          <ReviewQueuePage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("ReviewQueuePage", () => {
  it("lists queued analyses with their SLA age", () => {
    useSessionMock.mockReturnValue({ data: { id: "u1", capabilities: ["analysis:view"] } });
    useReviewQueueMock.mockReturnValue({ data: [ENTRY], isPending: false, error: null });
    useAssignReviewerMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: { id: "v1", product_id: "p1", version_no: 2 } });

    renderPage();

    expect(screen.getByText("Masala Chips v2")).toBeInTheDocument();
    expect(screen.getByText("Needs review")).toBeInTheDocument();
    expect(screen.getByText("1h 30m")).toBeInTheDocument();
    expect(screen.getByText("Unassigned")).toBeInTheDocument();
  });

  it("shows an empty state when nothing is waiting", () => {
    useSessionMock.mockReturnValue({ data: { id: "u1", capabilities: ["analysis:view"] } });
    useReviewQueueMock.mockReturnValue({ data: [], isPending: false, error: null });
    useAssignReviewerMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useProductMock.mockReturnValue({ data: undefined });
    useProductVersionMock.mockReturnValue({ data: undefined });

    renderPage();

    expect(screen.getByText("Nothing is waiting for review.")).toBeInTheDocument();
  });

  it("hides the assign control from a viewer without finding:decide", () => {
    useSessionMock.mockReturnValue({ data: { id: "u1", capabilities: ["analysis:view"] } });
    useReviewQueueMock.mockReturnValue({ data: [ENTRY], isPending: false, error: null });
    useAssignReviewerMock.mockReturnValue({ mutate: vi.fn(), isPending: false });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: { id: "v1", product_id: "p1", version_no: 2 } });

    renderPage();

    expect(screen.queryByRole("button", { name: "Assign to me" })).not.toBeInTheDocument();
  });

  it("lets a reviewer assign the analysis to themselves", async () => {
    const mutate = vi.fn();
    useSessionMock.mockReturnValue({
      data: { id: "u1", capabilities: ["analysis:view", "finding:decide"] },
    });
    useReviewQueueMock.mockReturnValue({ data: [ENTRY], isPending: false, error: null });
    useAssignReviewerMock.mockReturnValue({ mutate, isPending: false });
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockReturnValue({ data: { id: "v1", product_id: "p1", version_no: 2 } });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "Assign to me" }));

    expect(mutate).toHaveBeenCalledWith(
      { reviewer_id: "u1" },
      expect.objectContaining({ onError: expect.any(Function) }),
    );
  });
});
