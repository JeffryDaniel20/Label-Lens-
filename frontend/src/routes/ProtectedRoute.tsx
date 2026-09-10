import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router-dom";

import { useSession } from "@/features/auth/session";

/** Redirects an unauthenticated visitor to `/login`, remembering where they
 * were headed (`state.from`) so a successful login can return them there -
 * the literal acceptance criterion: "unauthenticated access redirects." */
export function ProtectedRoute({ children }: { children: ReactNode }) {
  const { data: session, isPending } = useSession();
  const location = useLocation();

  if (isPending) {
    return <div className="flex h-screen items-center justify-center text-slate-500">Loading…</div>;
  }

  if (!session) {
    return <Navigate to="/login" replace state={{ from: location }} />;
  }

  return <>{children}</>;
}
