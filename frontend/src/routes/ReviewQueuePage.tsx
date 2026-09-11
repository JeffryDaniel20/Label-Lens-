import { Link } from "react-router-dom";

import { ApiError } from "@/api/client";
import type { ReviewQueueEntryOut } from "@/api/types";
import { useSession } from "@/features/auth/session";
import { useProduct } from "@/features/catalog/products";
import { useProductVersion } from "@/features/catalog/versions";
import { useAssignReviewer, useReviewQueue } from "@/features/review/review";
import { useToast } from "@/lib/toast";

/** Rendered relative to now on every re-render (queue polling/refetch keeps
 * it fresh enough) - no need for a ticking clock for a queue-age display. */
function formatAge(slaSince: string): string {
  const ms = Date.now() - new Date(slaSince).getTime();
  const minutes = Math.max(0, Math.round(ms / 60_000));
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ${minutes % 60}m`;
  const days = Math.floor(hours / 24);
  return `${days}d ${hours % 24}h`;
}

function QueueRow({ entry, canAssign }: { entry: ReviewQueueEntryOut; canAssign: boolean }) {
  const { analysis, sla_since: slaSince } = entry;
  const { data: version } = useProductVersion(analysis.product_version_id);
  const { data: product } = useProduct(version?.product_id);
  const { data: session } = useSession();
  const assignReviewer = useAssignReviewer(analysis.id);
  const { showToast } = useToast();

  const isAssignedToMe = analysis.assigned_reviewer_id === session?.id;
  const detailHref =
    product && version
      ? `/products/${product.id}/versions/${version.id}/analyses/${analysis.id}`
      : undefined;

  function handleAssign(reviewerId: string | null) {
    assignReviewer.mutate(
      { reviewer_id: reviewerId },
      {
        onError: (err) => {
          showToast(
            err instanceof ApiError ? err.message : "Could not update assignment.",
            "error",
          );
        },
      },
    );
  }

  return (
    <tr>
      <td className="px-4 py-2 font-medium text-slate-900">
        {detailHref ? (
          <Link to={detailHref} className="hover:underline">
            {product?.name ?? "…"} v{version?.version_no ?? "…"}
          </Link>
        ) : (
          "Loading…"
        )}
      </td>
      <td className="px-4 py-2 text-slate-700">
        <span className="rounded-full bg-amber-100 px-2.5 py-1 text-xs font-medium text-amber-800">
          {analysis.state === "needs_review" ? "Needs review" : "In review"}
        </span>
      </td>
      <td className="px-4 py-2 text-slate-700">{formatAge(slaSince)}</td>
      <td className="px-4 py-2 text-slate-700">
        {analysis.assigned_reviewer_id
          ? analysis.assigned_reviewer_id === session?.id
            ? "You"
            : "Assigned"
          : "Unassigned"}
      </td>
      <td className="px-4 py-2">
        {canAssign && (
          <button
            type="button"
            disabled={assignReviewer.isPending}
            onClick={() => handleAssign(isAssignedToMe ? null : (session?.id ?? null))}
            className="rounded-md border border-slate-300 px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-50 disabled:opacity-50"
          >
            {isAssignedToMe ? "Unassign" : "Assign to me"}
          </button>
        )}
      </td>
    </tr>
  );
}

export function ReviewQueuePage() {
  const { data: entries, isPending, error } = useReviewQueue();
  const { data: session } = useSession();
  const capabilities = new Set(session?.capabilities ?? []);
  const canAssign = capabilities.has("finding:decide");

  return (
    <div>
      <h1 className="text-2xl font-semibold text-slate-900">Review queue</h1>
      <p className="mt-1 text-sm text-slate-600">
        Analyses awaiting or in review, oldest-waiting-first.
      </p>

      {isPending && <p className="mt-4 text-slate-600">Loading queue…</p>}
      {error && <p className="mt-4 text-red-700">Could not load the review queue.</p>}
      {entries && entries.length === 0 && (
        <p className="mt-4 text-slate-600">Nothing is waiting for review.</p>
      )}
      {entries && entries.length > 0 && (
        <div className="mt-4 overflow-x-auto rounded-md border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Analysis</th>
                <th className="px-4 py-2">Status</th>
                <th className="px-4 py-2">Waiting</th>
                <th className="px-4 py-2">Reviewer</th>
                <th className="px-4 py-2" />
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {entries.map((entry) => (
                <QueueRow key={entry.analysis.id} entry={entry} canAssign={canAssign} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
