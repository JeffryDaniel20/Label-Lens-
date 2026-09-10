import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { RootLayout } from "@/routes/RootLayout";

const { useSessionMock, useLogoutMock } = vi.hoisted(() => ({
  useSessionMock: vi.fn(),
  useLogoutMock: vi.fn(),
}));
vi.mock("@/features/auth/session", () => ({
  useSession: useSessionMock,
  useLogout: useLogoutMock,
}));

function renderLayout() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<RootLayout />}>
            <Route index element={<div>Home</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RootLayout nav", () => {
  it("only shows links the session's capabilities actually grant", () => {
    useSessionMock.mockReturnValue({
      data: { display_name: "Val", role: "viewer", capabilities: ["product:view"] },
    });
    useLogoutMock.mockReturnValue({ mutate: vi.fn() });

    renderLayout();

    expect(screen.getByText("Products")).toBeInTheDocument();
    expect(screen.queryByText("Members")).not.toBeInTheDocument();
    expect(screen.queryByText("Audit log")).not.toBeInTheDocument();
  });

  it("shows the full nav for a role with every capability", () => {
    useSessionMock.mockReturnValue({
      data: {
        display_name: "Owner",
        role: "owner",
        capabilities: ["product:view", "analysis:view", "report:view", "member:view", "audit:view"],
      },
    });
    useLogoutMock.mockReturnValue({ mutate: vi.fn() });

    renderLayout();

    for (const label of ["Products", "Analyses", "Reports", "Members", "Audit log"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
  });

  it("offers sign out only when a session exists", () => {
    useSessionMock.mockReturnValue({ data: null });
    useLogoutMock.mockReturnValue({ mutate: vi.fn() });

    renderLayout();

    expect(screen.queryByText("Sign out")).not.toBeInTheDocument();
  });
});
