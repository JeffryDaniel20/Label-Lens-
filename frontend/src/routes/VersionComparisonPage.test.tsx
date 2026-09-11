import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { VersionComparisonPage } from "@/routes/VersionComparisonPage";
import { ToastProvider } from "@/lib/toast";

const { useVersionComparisonMock, useProductMock, useProductVersionMock } = vi.hoisted(() => ({
  useVersionComparisonMock: vi.fn(),
  useProductMock: vi.fn(),
  useProductVersionMock: vi.fn(),
}));
vi.mock("@/features/analysis/comparison", () => ({
  useVersionComparison: useVersionComparisonMock,
}));
vi.mock("@/features/catalog/products", () => ({ useProduct: useProductMock }));
vi.mock("@/features/catalog/versions", () => ({ useProductVersion: useProductVersionMock }));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/products/p1/compare/v1/v2"]}>
          <Routes>
            <Route
              path="/products/:productId/compare/:fromVersionId/:toVersionId"
              element={<VersionComparisonPage />}
            />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

const BASE_COMPARISON = {
  from_version_id: "v1",
  to_version_id: "v2",
  from_analysis_id: "a1",
  to_analysis_id: "a2",
  from_ruleset_version_id: "r1",
  to_ruleset_version_id: "r1",
  common_ruleset_applied: false,
  ruleset_changed: false,
  field_diffs: [],
  finding_diffs: [],
};

describe("VersionComparisonPage", () => {
  beforeEach(() => {
    useProductMock.mockReturnValue({ data: { id: "p1", name: "Masala Chips" } });
    useProductVersionMock.mockImplementation((id: string | undefined) => ({
      data: id === "v1" ? { id: "v1", version_no: 1 } : { id: "v2", version_no: 2 },
    }));
  });

  it("shows a real field and finding diff", () => {
    useVersionComparisonMock.mockReturnValue({
      data: {
        ...BASE_COMPARISON,
        field_diffs: [
          {
            field_path: "allergens.declared",
            from_value: ["Wheat"],
            to_value: ["Unobtainium"],
            change: "changed",
          },
          {
            field_path: "quantity.net_quantity",
            from_value: "250 g",
            to_value: "250 g",
            change: "unchanged",
          },
        ],
        finding_diffs: [
          {
            rule_key: "IN-FSSAI-FOOD-ALLERGEN-NAMES-RECOGNIZED",
            rule_title: "Declared allergens are recognized allergen names",
            from_status: "pass",
            to_status: "fail",
            from_severity: "major",
            to_severity: "major",
            change: "changed",
            cause: "label_change",
          },
        ],
      },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText("allergens.declared")).toBeInTheDocument();
    expect(
      screen.getByText("Declared allergens are recognized allergen names"),
    ).toBeInTheDocument();
    expect(screen.getByText("Label change")).toBeInTheDocument();
    expect(screen.getByText("v1 → v2")).toBeInTheDocument();
  });

  it("shows an empty state when nothing changed", () => {
    useVersionComparisonMock.mockReturnValue({
      data: {
        ...BASE_COMPARISON,
        field_diffs: [
          {
            field_path: "quantity.net_quantity",
            from_value: "250 g",
            to_value: "250 g",
            change: "unchanged",
          },
        ],
        finding_diffs: [
          {
            rule_key: "IN-FSSAI-FOOD-NET-QUANTITY-DECLARED",
            rule_title: "Net quantity is declared",
            from_status: "pass",
            to_status: "pass",
            from_severity: "critical",
            to_severity: "critical",
            change: "unchanged",
            cause: "unchanged",
          },
        ],
      },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText("No extracted field values changed.")).toBeInTheDocument();
    expect(screen.getByText("No compliance findings changed.")).toBeInTheDocument();
  });

  it("warns when the ruleset changed between the two versions", () => {
    useVersionComparisonMock.mockReturnValue({
      data: { ...BASE_COMPARISON, to_ruleset_version_id: "r2", ruleset_changed: true },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText(/pinned to different ruleset versions/)).toBeInTheDocument();
  });

  it("re-requests the comparison with common_ruleset when the toggle is checked", async () => {
    useVersionComparisonMock.mockReturnValue({
      data: { ...BASE_COMPARISON, ruleset_changed: true },
      isPending: false,
      error: null,
    });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByLabelText("Compare under common ruleset"));

    expect(useVersionComparisonMock).toHaveBeenLastCalledWith("v1", "v2", true);
  });

  it("shows an honest empty state when neither version has been analyzed", () => {
    useVersionComparisonMock.mockReturnValue({
      data: { ...BASE_COMPARISON, from_analysis_id: null, to_analysis_id: null },
      isPending: false,
      error: null,
    });

    renderPage();

    expect(screen.getByText("Neither version has been analyzed yet.")).toBeInTheDocument();
  });
});
