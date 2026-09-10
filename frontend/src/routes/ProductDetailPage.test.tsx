import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProductDetailPage } from "@/routes/ProductDetailPage";
import { ToastProvider } from "@/lib/toast";

const { useProductMock, useProductVersionsMock, useCreateVersionMock, useSessionMock } = vi.hoisted(
  () => ({
    useProductMock: vi.fn(),
    useProductVersionsMock: vi.fn(),
    useCreateVersionMock: vi.fn(),
    useSessionMock: vi.fn(),
  }),
);
vi.mock("@/features/catalog/products", () => ({ useProduct: useProductMock }));
vi.mock("@/features/catalog/versions", () => ({
  useProductVersions: useProductVersionsMock,
  useCreateVersion: useCreateVersionMock,
}));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/products/p1"]}>
          <Routes>
            <Route path="/products/:productId" element={<ProductDetailPage />} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("ProductDetailPage", () => {
  it("lists label versions with newest-first status chips", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view"] } });
    useProductMock.mockReturnValue({
      data: {
        id: "p1",
        name: "Masala Chips",
        internal_sku: "MC-001",
        category_hint: null,
        market_codes: [],
      },
      isPending: false,
      error: null,
    });
    useProductVersionsMock.mockReturnValue({
      data: [
        {
          id: "v2",
          product_id: "p1",
          version_no: 2,
          label: "Relaunch",
          status: "draft",
          locked_at: null,
          superseded_at: null,
          created_at: "2026-02-01T00:00:00Z",
        },
        {
          id: "v1",
          product_id: "p1",
          version_no: 1,
          label: "",
          status: "superseded",
          locked_at: null,
          superseded_at: "2026-02-01T00:00:00Z",
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
    });
    useCreateVersionMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderPage();

    expect(screen.getByText("v2 — Relaunch")).toBeInTheDocument();
    expect(screen.getByText("v1")).toBeInTheDocument();
    expect(screen.getByText("draft")).toBeInTheDocument();
    expect(screen.getByText("superseded")).toBeInTheDocument();
  });

  it("creates a new version when a manager submits the form", async () => {
    const mutate = vi.fn();
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "product:manage"] } });
    useProductMock.mockReturnValue({
      data: {
        id: "p1",
        name: "Masala Chips",
        internal_sku: "MC-001",
        category_hint: null,
        market_codes: [],
      },
      isPending: false,
      error: null,
    });
    useProductVersionsMock.mockReturnValue({ data: [] });
    useCreateVersionMock.mockReturnValue({ mutate, isPending: false });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New version" }));
    await user.type(screen.getByLabelText("Label (optional)"), "Diwali 2026");
    await user.click(screen.getByRole("button", { name: "Create version" }));

    expect(mutate).toHaveBeenCalledWith(
      { label: "Diwali 2026" },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });
});
