/** A page whose real implementation is a later Phase 6 task (P6-T2+) -
 * present here only so a role-gated nav link has somewhere real to go,
 * rather than a 404, once P6-T1's own scope (app shell, auth, routing)
 * makes that link visible. */
export function PlaceholderPage({ title }: { title: string }) {
  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">{title}</h1>
      <p className="mt-2 text-slate-600">Not built yet.</p>
    </div>
  );
}
