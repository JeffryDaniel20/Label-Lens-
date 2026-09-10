import { useSession } from "@/features/auth/session";

export function DashboardPage() {
  const { data: session } = useSession();

  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">
        Welcome{session ? `, ${session.display_name}` : ""}
      </h1>
      <p className="mt-2 text-slate-600">
        You are signed in as <span className="font-medium">{session?.role}</span>.
      </p>
    </div>
  );
}
