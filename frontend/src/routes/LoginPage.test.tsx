import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { describe, expect, it, vi } from "vitest";

import { LoginPage } from "@/routes/LoginPage";
import { ToastProvider } from "@/lib/toast";

const { useSessionMock, useLoginMock } = vi.hoisted(() => ({
  useSessionMock: vi.fn(),
  useLoginMock: vi.fn(),
}));
vi.mock("@/features/auth/session", () => ({
  useSession: useSessionMock,
  useLogin: useLoginMock,
}));

function renderPage() {
  const client = new QueryClient();
  return render(
    <QueryClientProvider client={client}>
      <ToastProvider>
        <MemoryRouter initialEntries={["/login"]}>
          <Routes>
            <Route path="/login" element={<LoginPage />} />
            <Route path="/" element={<div>Dashboard</div>} />
          </Routes>
        </MemoryRouter>
      </ToastProvider>
    </QueryClientProvider>,
  );
}

describe("LoginPage", () => {
  it("submits email and password to the login mutation", async () => {
    const mutate = vi.fn();
    useSessionMock.mockReturnValue({ data: null });
    useLoginMock.mockReturnValue({ mutate, isPending: false });

    renderPage();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "owner@acmefoods.com");
    await user.type(screen.getByLabelText("Password"), "CorrectHorse42!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));

    expect(mutate).toHaveBeenCalledWith(
      {
        email: "owner@acmefoods.com",
        password: "CorrectHorse42!",
        totp_code: null,
        recovery_code: null,
      },
      expect.objectContaining({ onSuccess: expect.any(Function), onError: expect.any(Function) }),
    );
  });

  it("redirects away from /login when already signed in", () => {
    useSessionMock.mockReturnValue({ data: { id: "u1" } });
    useLoginMock.mockReturnValue({ mutate: vi.fn(), isPending: false });

    renderPage();

    expect(screen.getByText("Dashboard")).toBeInTheDocument();
  });

  it("reveals the MFA code field once the backend reports mfa_required", async () => {
    let onSuccess: ((data: { mfa_required: boolean }) => void) | undefined;
    useSessionMock.mockReturnValue({ data: null });
    useLoginMock.mockReturnValue({
      mutate: (_payload: unknown, options: { onSuccess: typeof onSuccess }) => {
        onSuccess = options.onSuccess;
      },
      isPending: false,
    });

    renderPage();
    const user = userEvent.setup();
    await user.type(screen.getByLabelText("Email"), "owner@acmefoods.com");
    await user.type(screen.getByLabelText("Password"), "CorrectHorse42!");
    await user.click(screen.getByRole("button", { name: "Sign in" }));
    act(() => {
      onSuccess?.({ mfa_required: true });
    });

    expect(await screen.findByLabelText("Authentication code")).toBeInTheDocument();
  });
});
