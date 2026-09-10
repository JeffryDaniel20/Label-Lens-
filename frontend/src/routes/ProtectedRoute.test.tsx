import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { ProtectedRoute } from "@/routes/ProtectedRoute";

const { useSessionMock } = vi.hoisted(() => ({ useSessionMock: vi.fn() }));
vi.mock("@/features/auth/session", () => ({ useSession: useSessionMock }));

function renderAt(path: string) {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/login" element={<div>Login page</div>} />
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <div>Protected content</div>
              </ProtectedRoute>
            }
          />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("ProtectedRoute", () => {
  it("redirects to /login when there is no session", () => {
    useSessionMock.mockReturnValue({ data: null, isPending: false });

    renderAt("/");

    expect(screen.getByText("Login page")).toBeInTheDocument();
    expect(screen.queryByText("Protected content")).not.toBeInTheDocument();
  });

  it("renders the protected content when a session exists", () => {
    useSessionMock.mockReturnValue({ data: { id: "u1" }, isPending: false });

    renderAt("/");

    expect(screen.getByText("Protected content")).toBeInTheDocument();
  });

  it("shows a loading state instead of redirecting while the session is pending", () => {
    useSessionMock.mockReturnValue({ data: undefined, isPending: true });

    renderAt("/");

    expect(screen.getByText("Loading…")).toBeInTheDocument();
    expect(screen.queryByText("Login page")).not.toBeInTheDocument();
  });
});
