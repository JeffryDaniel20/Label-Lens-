import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import type { FindingDiffOut } from "@/api/types";
import { useVersionComparison } from "@/features/analysis/comparison";
import { useProduct } from "@/features/catalog/products";
import { useProductVersion } from "@/features/catalog/versions";

const CHANGE_STYLES: Record<string, string> = {
  added: "bg-blue-100 text-blue-800",
  removed: "bg-slate-200 text-slate-700",
  changed: "bg-amber-100 text-amber-800",
  unchanged: "bg-slate-100 text-slate-500",
};

const CAUSE_LABELS: Record<FindingDiffOut["cause"], string> = {
  label_change: "Label change",
  rule_change: "Rule change",
  extraction_change: "Extraction change",
  unchanged: "—",
};

const CAUSE_STYLES: Record<FindingDiffOut["cause"], string> = {
  label_change: "bg-purple-100 text-purple-800",
  rule_change: "bg-orange-100 text-orange-800",
  extraction_change: "bg-sky-100 text-sky-800",
  unchanged: "bg-slate-100 text-slate-500",
};

function formatValue(value: unknown): string {
  if (value === null || value === undefined) return "—";
  return typeof value === "string" ? value : JSON.stringify(value);
}

export function VersionComparisonPage() {
  const { productId, fromVersionId, toVersionId } = useParams<{
    productId: string;
    fromVersionId: string;
    toVersionId: string;
  }>();
  const { data: product } = useProduct(productId);
  const { data: fromVersion } = useProductVersion(fromVersionId);
  const { data: toVersion } = useProductVersion(toVersionId);
  const [commonRuleset, setCommonRuleset] = useState(false);
  const {
    data: comparison,
    isPending,
    error,
  } = useVersionComparison(fromVersionId, toVersionId, commonRuleset);

  const changedFieldDiffs = (comparison?.field_diffs ?? []).filter((d) => d.change !== "unchanged");
  const changedFindingDiffs = (comparison?.finding_diffs ?? []).filter(
    (d) => d.change !== "unchanged",
  );

  return (
    <div>
      <Link to={`/products/${productId}`} className="text-sm text-slate-500 hover:text-slate-700">
        &larr; {product?.name ?? "Product"}
      </Link>

      <h1 className="mt-2 text-2xl font-semibold text-slate-900">Compare versions</h1>
      <p className="mt-1 text-sm text-slate-600">
        v{fromVersion?.version_no ?? "…"} &rarr; v{toVersion?.version_no ?? "…"}
      </p>

      <label className="mt-4 flex items-center gap-2 text-sm text-slate-700">
        <input
          type="checkbox"
          checked={commonRuleset}
          onChange={(event) => setCommonRuleset(event.target.checked)}
        />
        Compare under common ruleset
      </label>
      {comparison && comparison.ruleset_changed && (
        <p className="mt-1 text-xs text-slate-500">
          {commonRuleset
            ? "Both versions were re-evaluated against the newer version's own ruleset."
            : "These two versions were pinned to different ruleset versions - some finding differences below may stem from the rules themselves changing, not the label."}
        </p>
      )}

      {isPending && <p className="mt-4 text-slate-600">Loading comparison…</p>}
      {error && <p className="mt-4 text-red-700">Could not load this comparison.</p>}

      {comparison && !comparison.from_analysis_id && !comparison.to_analysis_id && (
        <p className="mt-4 text-slate-600">Neither version has been analyzed yet.</p>
      )}

      {comparison && (
        <>
          <h2 className="mt-8 text-lg font-medium text-slate-900">Field changes</h2>
          {changedFieldDiffs.length === 0 && (
            <p className="mt-2 text-slate-600">No extracted field values changed.</p>
          )}
          {changedFieldDiffs.length > 0 && (
            <div className="mt-3 overflow-x-auto rounded-md border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">Field</th>
                    <th className="px-4 py-2">From</th>
                    <th className="px-4 py-2">To</th>
                    <th className="px-4 py-2">Change</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {changedFieldDiffs.map((d) => (
                    <tr key={d.field_path}>
                      <td className="px-4 py-2 font-medium text-slate-900">{d.field_path}</td>
                      <td className="px-4 py-2 text-slate-700">{formatValue(d.from_value)}</td>
                      <td className="px-4 py-2 text-slate-700">{formatValue(d.to_value)}</td>
                      <td className="px-4 py-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-medium ${CHANGE_STYLES[d.change]}`}
                        >
                          {d.change}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}

          <h2 className="mt-8 text-lg font-medium text-slate-900">Finding changes</h2>
          {changedFindingDiffs.length === 0 && comparison.finding_diffs.length > 0 && (
            <p className="mt-2 text-slate-600">No compliance findings changed.</p>
          )}
          {comparison.finding_diffs.length === 0 && (
            <p className="mt-2 text-slate-600">No findings to compare.</p>
          )}
          {changedFindingDiffs.length > 0 && (
            <div className="mt-3 overflow-x-auto rounded-md border border-slate-200 bg-white">
              <table className="w-full text-left text-sm">
                <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
                  <tr>
                    <th className="px-4 py-2">Rule</th>
                    <th className="px-4 py-2">From</th>
                    <th className="px-4 py-2">To</th>
                    <th className="px-4 py-2">Cause</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-slate-200">
                  {changedFindingDiffs.map((d) => (
                    <tr key={d.rule_key}>
                      <td className="px-4 py-2 font-medium text-slate-900">
                        {d.rule_title ?? d.rule_key}
                      </td>
                      <td className="px-4 py-2 text-slate-700">{d.from_status ?? "—"}</td>
                      <td className="px-4 py-2 text-slate-700">{d.to_status ?? "—"}</td>
                      <td className="px-4 py-2">
                        <span
                          className={`rounded-full px-2 py-0.5 text-xs font-medium ${CAUSE_STYLES[d.cause]}`}
                        >
                          {CAUSE_LABELS[d.cause]}
                        </span>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </>
      )}
    </div>
  );
}
