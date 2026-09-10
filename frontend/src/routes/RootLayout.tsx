import { NavLink, Outlet } from "react-router-dom";

import { useLogout, useSession } from "@/features/auth/session";

interface NavItem {
  label: string;
  to: string;
  /** Shown only when the session's own `capabilities` (from `GET /v1/me`,
   * sourced from `app.identity.rbac.capabilities_for`) includes this - the
   * literal acceptance criterion: "role-gated nav reflects capabilities." */
  requires: string;
}

const NAV_ITEMS: NavItem[] = [
  { label: "Products", to: "/products", requires: "product:view" },
  { label: "Analyses", to: "/analyses", requires: "analysis:view" },
  { label: "Reports", to: "/reports", requires: "report:view" },
  { label: "Members", to: "/settings/members", requires: "member:view" },
  { label: "Audit log", to: "/audit", requires: "audit:view" },
];

export function RootLayout() {
  const { data: session } = useSession();
  const logout = useLogout();
  const capabilities = new Set(session?.capabilities ?? []);

  return (
    <div className="min-h-screen bg-slate-50">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-6xl items-center justify-between px-6 py-3">
          <span className="text-lg font-semibold text-slate-900">LabelLens</span>
          <nav className="flex items-center gap-4">
            {NAV_ITEMS.filter((item) => capabilities.has(item.requires)).map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                className={({ isActive }) =>
                  `text-sm font-medium ${isActive ? "text-slate-900" : "text-slate-500 hover:text-slate-700"}`
                }
              >
                {item.label}
              </NavLink>
            ))}
            {session && (
              <button
                type="button"
                onClick={() => logout.mutate()}
                className="text-sm font-medium text-slate-500 hover:text-slate-700"
              >
                Sign out
              </button>
            )}
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
