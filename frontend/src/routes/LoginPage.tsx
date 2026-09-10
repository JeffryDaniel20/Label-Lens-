import { useState, type FormEvent } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { ApiError } from "@/api/client";
import { useLogin, useSession } from "@/features/auth/session";
import { useToast } from "@/lib/toast";

export function LoginPage() {
  const { data: session } = useSession();
  const location = useLocation();
  const login = useLogin();
  const { showToast } = useToast();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [totpCode, setTotpCode] = useState("");
  const [mfaRequired, setMfaRequired] = useState(false);

  if (session) {
    const from = (location.state as { from?: Location } | null)?.from;
    return <Navigate to={from?.pathname ?? "/"} replace />;
  }

  function handleSubmit(event: FormEvent) {
    event.preventDefault();
    login.mutate(
      { email, password, totp_code: mfaRequired ? totpCode : null, recovery_code: null },
      {
        onSuccess: (data) => {
          if (data.mfa_required) {
            setMfaRequired(true);
            return;
          }
          showToast("Signed in.", "success");
        },
        onError: (error) => {
          const message =
            error instanceof ApiError ? error.message : "Something went wrong. Try again.";
          showToast(message, "error");
        },
      },
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-slate-50">
      <form
        onSubmit={handleSubmit}
        className="w-full max-w-sm rounded-lg border border-slate-200 bg-white p-8 shadow-sm"
      >
        <h1 className="mb-6 text-xl font-semibold text-slate-900">Sign in to LabelLens</h1>

        <label className="mb-4 block">
          <span className="mb-1 block text-sm font-medium text-slate-700">Email</span>
          <input
            type="email"
            required
            autoComplete="email"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
          />
        </label>

        <label className="mb-4 block">
          <span className="mb-1 block text-sm font-medium text-slate-700">Password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
          />
        </label>

        {mfaRequired && (
          <label className="mb-4 block">
            <span className="mb-1 block text-sm font-medium text-slate-700">
              Authentication code
            </span>
            <input
              type="text"
              inputMode="numeric"
              autoComplete="one-time-code"
              required
              value={totpCode}
              onChange={(e) => setTotpCode(e.target.value)}
              className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm focus:border-slate-500 focus:outline-none"
            />
          </label>
        )}

        <button
          type="submit"
          disabled={login.isPending}
          className="w-full rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-800 disabled:opacity-50"
        >
          {login.isPending ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
