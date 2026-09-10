import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProductsPage } from "@/routes/ProductsPage";
import { ToastProvider } from "@/lib/toast";

const { useProductsMock, useCreateProductMock, useSessionMock } = vi.hoisted(() => ({
  useProductsMock: vi.fn(),
  useCreateProductMock: vi.fn(),
  useSessionMock: vi.fn(),
}));
vi.mock("@/features/catalog/products", () => ({
  useProducts: useProductsMock,
  useCreateProduct: useCreateProductMock,
}));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/products"]}>
          <ProductsPage />
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("ProductsPage", () => {
  it("lists products with their SKU and market codes", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view"] } });
    useProductsMock.mockReturnValue({
      data: [
        {
          id: "p1",
          name: "Masala Chips",
          internal_sku: "MC-001",
          category_hint: null,
          market_codes: ["IN"],
          created_at: "2026-01-01T00:00:00Z",
        },
      ],
      isPending: false,
      error: null,
    });
    useCreateProductMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderPage();

    expect(screen.getByText("Masala Chips")).toBeInTheDocument();
    expect(screen.getByText("SKU MC-001")).toBeInTheDocument();
    expect(screen.getByText("IN")).toBeInTheDocument();
  });

  it("hides the create-product control from a viewer without product:manage", () => {
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view"] } });
    useProductsMock.mockReturnValue({ data: [], isPending: false, error: null });
    useCreateProductMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderPage();

    expect(screen.queryByRole("button", { name: "New product" })).not.toBeInTheDocument();
  });

  it("submits the create-product form for a user who can manage products", async () => {
    const mutate = vi.fn();
    useSessionMock.mockReturnValue({ data: { capabilities: ["product:view", "product:manage"] } });
    useProductsMock.mockReturnValue({ data: [], isPending: false, error: null });
    useCreateProductMock.mockReturnValue({ mutate, isPending: false });

    renderPage();
    const user = userEvent.setup();
    await user.click(screen.getByRole("button", { name: "New product" }));
    await user.type(screen.getByLabelText("Name"), "Masala Chips");
    await user.type(screen.getByLabelText("Internal SKU"), "MC-001");
    await user.type(screen.getByLabelText("Market codes (comma separated)"), "IN, US");
    await user.click(screen.getByRole("button", { name: "Create product" }));

    expect(mutate).toHaveBeenCalledWith(
      { name: "Masala Chips", internal_sku: "MC-001", market_codes: ["IN", "US"] },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });
});
