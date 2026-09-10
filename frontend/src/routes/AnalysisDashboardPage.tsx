import { useState } from "react";
import { Link, useParams } from "react-router-dom";

import { ApiError } from "@/api/client";
import type {
  AnalysisState,
  ConfidenceTier,
  FindingStatus,
  ReportSnapshot,
  Severity,
} from "@/api/types";
import { LabelViewer } from "@/components/LabelViewer";
import {
  useAnalysis,
  useAnalysisEvents,
  useFindings,
  useGenerateReport,
  useReports,
} from "@/features/analysis/analysis";
import { useAnalysisSse } from "@/features/analysis/useAnalysisSse";
import { useSession } from "@/features/auth/session";
import { useProduct } from "@/features/catalog/products";
import { useProductVersion } from "@/features/catalog/versions";
import { useToast } from "@/lib/toast";

const STATE_LABELS: Record<AnalysisState, string> = {
  queued: "Queued",
  validating: "Validating upload",
  preprocessing: "Preprocessing",
  ocr: "Reading label (OCR)",
  extracting: "Extracting label data",
  verifying_evidence: "Verifying evidence",
  normalizing: "Normalizing extracted values",
  classifying: "Classifying product",
  rule_eval: "Evaluating rules",
  scoring: "Scoring confidence",
  needs_review: "Needs review",
  review: "In review",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Cancelled",
};

const STATE_STYLES: Record<AnalysisState, string> = {
  queued: "bg-slate-100 text-slate-700",
  validating: "bg-blue-100 text-blue-800",
  preprocessing: "bg-blue-100 text-blue-800",
  ocr: "bg-blue-100 text-blue-800",
  extracting: "bg-blue-100 text-blue-800",
  verifying_evidence: "bg-blue-100 text-blue-800",
  normalizing: "bg-blue-100 text-blue-800",
  classifying: "bg-blue-100 text-blue-800",
  rule_eval: "bg-blue-100 text-blue-800",
  scoring: "bg-blue-100 text-blue-800",
  needs_review: "bg-amber-100 text-amber-800",
  review: "bg-amber-100 text-amber-800",
  completed: "bg-emerald-100 text-emerald-800",
  failed: "bg-red-100 text-red-800",
  cancelled: "bg-slate-200 text-slate-700",
};

const TIER_STYLES: Record<ConfidenceTier, string> = {
  high: "bg-emerald-100 text-emerald-800",
  medium: "bg-amber-100 text-amber-800",
  low: "bg-red-100 text-red-800",
};

const SEVERITY_STYLES: Record<Severity, string> = {
  critical: "bg-red-100 text-red-800",
  major: "bg-orange-100 text-orange-800",
  minor: "bg-amber-100 text-amber-800",
  advisory: "bg-slate-100 text-slate-700",
};

const FINDING_STATUS_STYLES: Record<FindingStatus, string> = {
  pass: "bg-emerald-100 text-emerald-800",
  fail: "bg-red-100 text-red-800",
  insufficient_data: "bg-amber-100 text-amber-800",
  not_applicable: "bg-slate-100 text-slate-700",
};

function formatCents(cents: number): string {
  return `$${(cents / 100).toFixed(4)}`;
}

export function AnalysisDashboardPage() {
  const { productId, versionId, analysisId } = useParams<{
    productId: string;
    versionId: string;
    analysisId: string;
  }>();
  const { data: product } = useProduct(productId);
  const { data: version } = useProductVersion(versionId);
  const { data: analysis, isPending, error } = useAnalysis(analysisId);
  const { data: events } = useAnalysisEvents(analysisId);
  const { data: findings } = useFindings(analysisId);
  const { data: reports } = useReports(analysisId);
  const { data: session } = useSession();
  const canGenerateReport = new Set(session?.capabilities ?? []).has("report:generate");
  const { connected } = useAnalysisSse(analysisId);
  const generateReport = useGenerateReport(analysisId ?? "");
  const { showToast } = useToast();
  const [selectedFindingId, setSelectedFindingId] = useState<string | undefined>(undefined);

  if (isPending) return <p className="text-slate-600">Loading analysis…</p>;
  if (error || !analysis) return <p className="text-red-700">Could not load this analysis.</p>;

  const sortedEvents = [...(events ?? [])].sort((a, b) => a.sequence - b.sequence);
  const failureEvent = sortedEvents.findLast((event) => event.to_state === "failed");
  const latestReport = [...(reports ?? [])].sort((a, b) =>
    b.generated_at.localeCompare(a.generated_at),
  )[0];
  const snapshot = latestReport?.snapshot as ReportSnapshot | undefined;
  const fieldTable = snapshot?.appendix?.extracted_fields ?? [];
  const isLive = !["completed", "failed", "cancelled", "needs_review", "review"].includes(
    analysis.state,
  );

  function handleGenerateReport() {
    generateReport.mutate(undefined, {
      onSuccess: () => showToast("Extraction report generated.", "success"),
      onError: (err) => {
        showToast(err instanceof ApiError ? err.message : "Could not generate report.", "error");
      },
    });
  }

  return (
    <div>
      <Link
        to={`/products/${productId}/versions/${versionId}`}
        className="text-sm text-slate-500 hover:text-slate-700"
      >
        &larr; {product?.name ?? "Product"} v{version?.version_no ?? ""}
      </Link>

      <div className="mt-2 flex items-center justify-between">
        <h1 className="text-2xl font-semibold text-slate-900">Analysis</h1>
        {isLive && (
          <span className="flex items-center gap-1.5 text-xs text-slate-500">
            <span
              className={`h-2 w-2 rounded-full ${connected ? "bg-emerald-500" : "bg-slate-300"}`}
              aria-hidden="true"
            />
            {connected ? "Live" : "Reconnecting…"}
          </span>
        )}
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2" aria-label="Analysis status">
        <span
          className={`rounded-full px-2.5 py-1 text-sm font-medium ${STATE_STYLES[analysis.state]}`}
        >
          {STATE_LABELS[analysis.state]}
        </span>
        {analysis.confidence_tier && (
          <span
            className={`rounded-full px-2.5 py-1 text-sm font-medium ${TIER_STYLES[analysis.confidence_tier]}`}
          >
            {analysis.confidence_tier} confidence
          </span>
        )}
      </div>

      <div className="mt-4 h-2 w-full max-w-md rounded-full bg-slate-200">
        <div
          className="h-2 rounded-full bg-slate-700 transition-all"
          style={{ width: `${analysis.progress_percentage}%` }}
        />
      </div>
      <p className="mt-1 text-sm text-slate-500">{analysis.progress_percentage}% complete</p>

      {analysis.state === "failed" && (
        <div className="mt-4 rounded-md border border-red-200 bg-red-50 p-4">
          <p className="font-medium text-red-800">
            Analysis failed{analysis.failure_stage ? ` at "${analysis.failure_stage}"` : ""}.
          </p>
          {failureEvent?.reason && (
            <p className="mt-1 text-sm text-red-700">{failureEvent.reason}</p>
          )}
          <p className="mt-1 text-sm text-red-700">
            {analysis.retryable
              ? "This failure was retried automatically and may succeed on a fresh submission."
              : "This failure was not automatically retryable."}
          </p>
        </div>
      )}

      {(analysis.total_tokens_in > 0 || analysis.total_tokens_out > 0) && (
        <p className="mt-4 text-sm text-slate-600">
          {analysis.total_tokens_in} tokens in / {analysis.total_tokens_out} tokens out
          {analysis.total_cost_cents > 0 && ` · ${formatCents(analysis.total_cost_cents)}`}
        </p>
      )}

      <h2 className="mt-8 text-lg font-medium text-slate-900">Progress timeline</h2>
      {sortedEvents.length === 0 && <p className="mt-2 text-slate-600">No events yet.</p>}
      {sortedEvents.length > 0 && (
        <ol className="mt-4 space-y-2" aria-label="Progress timeline">
          {sortedEvents.map((event) => (
            <li
              key={event.sequence}
              className="flex items-start justify-between rounded-md border border-slate-200 bg-white px-4 py-2 text-sm"
            >
              <div>
                <p className="font-medium text-slate-900">{STATE_LABELS[event.to_state]}</p>
                {event.reason && <p className="text-slate-600">{event.reason}</p>}
              </div>
              <time className="shrink-0 text-xs text-slate-500" dateTime={event.occurred_at}>
                {new Date(event.occurred_at).toLocaleTimeString()}
              </time>
            </li>
          ))}
        </ol>
      )}

      <h2 className="mt-8 text-lg font-medium text-slate-900">Label viewer</h2>
      {versionId && (
        <div className="mt-3">
          <LabelViewer versionId={versionId} selectedFindingId={selectedFindingId} />
        </div>
      )}

      <h2 className="mt-8 text-lg font-medium text-slate-900">Compliance findings</h2>
      {findings && findings.length === 0 && (
        <p className="mt-2 text-slate-600">
          No compliance findings are available for this analysis.
        </p>
      )}
      {findings && findings.length > 0 && (
        <ul className="mt-4 divide-y divide-slate-200 rounded-md border border-slate-200 bg-white">
          {findings.map((finding) => {
            const hasEvidence = finding.evidence_refs.length > 0;
            const isSelected = finding.id === selectedFindingId;
            return (
              <li key={finding.id}>
                <button
                  type="button"
                  disabled={!hasEvidence}
                  onClick={() => setSelectedFindingId(finding.id)}
                  aria-pressed={isSelected}
                  className={`flex w-full items-center justify-between px-4 py-3 text-left ${
                    hasEvidence ? "cursor-pointer hover:bg-slate-50" : "cursor-default"
                  } ${isSelected ? "bg-amber-50" : ""}`}
                >
                  <div>
                    <p className="font-medium text-slate-900">{finding.rule_key}</p>
                    {finding.message && <p className="text-sm text-slate-600">{finding.message}</p>}
                    {hasEvidence && (
                      <p className="mt-0.5 text-xs text-slate-500">
                        {isSelected ? "Showing evidence above ↑" : "Click to view evidence"}
                      </p>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-2">
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${SEVERITY_STYLES[finding.severity]}`}
                    >
                      {finding.severity}
                    </span>
                    <span
                      className={`rounded-full px-2 py-0.5 text-xs font-medium ${FINDING_STATUS_STYLES[finding.status]}`}
                    >
                      {finding.status}
                    </span>
                  </div>
                </button>
              </li>
            );
          })}
        </ul>
      )}

      <h2 className="mt-8 text-lg font-medium text-slate-900">Extracted information</h2>
      {canGenerateReport && (
        <button
          type="button"
          onClick={handleGenerateReport}
          disabled={generateReport.isPending}
          className="mt-3 rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white hover:bg-slate-700 disabled:opacity-50"
        >
          {generateReport.isPending
            ? "Generating…"
            : latestReport
              ? "Regenerate extraction report"
              : "Generate extraction report"}
        </button>
      )}
      {!latestReport && !canGenerateReport && (
        <p className="mt-2 text-slate-600">No extraction report has been generated yet.</p>
      )}
      {fieldTable.length > 0 && (
        <div className="mt-4 overflow-x-auto rounded-md border border-slate-200 bg-white">
          <table className="w-full text-left text-sm">
            <thead className="border-b border-slate-200 text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2">Field</th>
                <th className="px-4 py-2">Value</th>
                <th className="px-4 py-2">Confidence</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-200">
              {fieldTable.map((field) => (
                <tr key={field.field_path}>
                  <td className="px-4 py-2 font-medium text-slate-900">{field.field_path}</td>
                  <td className="px-4 py-2 text-slate-700">
                    {JSON.stringify(field.value_norm ?? field.value_raw)}
                  </td>
                  <td className="px-4 py-2 text-slate-700">
                    {(field.confidence * 100).toFixed(0)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
