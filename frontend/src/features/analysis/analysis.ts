import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { AnalysisEventOut, AnalysisOut, FindingOut, ReportOut } from "@/api/types";

/** Automated processing stops here (`app.analysis.stages.STOPPING_STATES`):
 * the three real terminals, plus the two human-review states nothing
 * advances automatically. Polling/SSE both stop once an analysis reaches
 * one of these - there is nothing further to wait for. */
const STOPPING_STATES = new Set(["completed", "failed", "cancelled", "needs_review", "review"]);

export function isStoppingState(state: string): boolean {
  return STOPPING_STATES.has(state);
}

export const analysisQueryKey = (analysisId: string) => ["analyses", analysisId] as const;
export const analysisEventsQueryKey = (analysisId: string) =>
  ["analyses", analysisId, "events"] as const;
export const findingsQueryKey = (analysisId: string) =>
  ["analyses", analysisId, "findings"] as const;
export const reportsQueryKey = (analysisId: string) => ["analyses", analysisId, "reports"] as const;

/** `POST .../analyses` is idempotent on the version's current file set
 * (`app.analysis.service.create_or_get_analysis`) - resubmitting for an
 * unchanged file set returns the SAME analysis (200) rather than erroring
 * or duplicating work, so this same mutation doubles as "view the current
 * analysis for this version" with no separate list-analyses endpoint
 * needed (none exists - see `app/analysis/router.py`). */
export function useSubmitAnalysis(versionId: string) {
  return useMutation({
    mutationFn: () => api.post<AnalysisOut>(`/v1/product-versions/${versionId}/analyses`),
  });
}

/** Polling fallback (P6-T3's own acceptance criterion: "SSE progress with
 * polling fallback") - refetches every 3s while the analysis hasn't
 * stopped, and stops polling once it has, so a reload or a broken SSE
 * connection never leaves the dashboard silently stale. */
export function useAnalysis(analysisId: string | undefined) {
  return useQuery({
    queryKey: analysisQueryKey(analysisId ?? ""),
    queryFn: () => api.get<AnalysisOut>(`/v1/analyses/${analysisId}`),
    enabled: Boolean(analysisId),
    refetchInterval: (query) => {
      const state = query.state.data?.state;
      return state && isStoppingState(state) ? false : 3000;
    },
  });
}

export function useAnalysisEvents(analysisId: string | undefined) {
  return useQuery({
    queryKey: analysisEventsQueryKey(analysisId ?? ""),
    queryFn: () => api.get<AnalysisEventOut[]>(`/v1/analyses/${analysisId}/events`),
    enabled: Boolean(analysisId),
  });
}

/** Real, possibly-empty by design: `rule_eval` is an honest no-op
 * placeholder (D-01, first jurisdiction, not yet decided), so no analysis
 * has ever had a `Finding` row written for it - `GET .../findings` returns
 * `[]` for every real analysis today, not an error. Rendered as-is, never
 * padded with invented findings. */
export function useFindings(analysisId: string | undefined) {
  return useQuery({
    queryKey: findingsQueryKey(analysisId ?? ""),
    queryFn: () => api.get<FindingOut[]>(`/v1/analyses/${analysisId}/findings`),
    enabled: Boolean(analysisId),
  });
}

export function useReports(analysisId: string | undefined) {
  return useQuery({
    queryKey: reportsQueryKey(analysisId ?? ""),
    queryFn: () => api.get<ReportOut[]>(`/v1/analyses/${analysisId}/reports`),
    enabled: Boolean(analysisId),
  });
}

export function useGenerateReport(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<ReportOut>(`/v1/analyses/${analysisId}/reports`),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: reportsQueryKey(analysisId) }),
  });
}
