import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type {
  AssignReviewerRequest,
  DecisionAction,
  FieldCorrectionRequest,
  FieldCorrectionResponse,
  FindingDecisionOut,
  ReviewQueueEntryOut,
  SignoffOut,
} from "@/api/types";
import { analysisQueryKey, findingsQueryKey } from "@/features/analysis/analysis";

export const findingDecisionsQueryKey = (findingId: string) =>
  ["findings", findingId, "decisions"] as const;

export function useFindingDecisions(findingId: string | undefined) {
  return useQuery({
    queryKey: findingDecisionsQueryKey(findingId ?? ""),
    queryFn: () => api.get<FindingDecisionOut[]>(`/v1/findings/${findingId}/decisions`),
    enabled: Boolean(findingId),
  });
}

/** Confirm/Override/Escalate (IMPLEMENTATION.md §12) - a decision is always
 * a new, appended row (`app.review.models.FindingDecision` is append-only),
 * never an edit to a prior one, so re-deciding the same finding just adds
 * another row rather than replacing anything. `findingId` is part of the
 * mutation's own variables (not the hook call) so one hook instance can
 * decide on whichever finding in a list was actually acted on, not just
 * the one it happened to be constructed for. */
export function useDecideFinding() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: ({
      findingId,
      action,
      reason,
    }: {
      findingId: string;
      action: DecisionAction;
      reason?: string;
    }) => api.post<FindingDecisionOut>(`/v1/findings/${findingId}/decision`, { action, reason }),
    onSuccess: (_data, variables) =>
      queryClient.invalidateQueries({ queryKey: findingDecisionsQueryKey(variables.findingId) }),
  });
}

/** Fix field (IMPLEMENTATION.md §12): corrects one field's value and
 * triggers a rule-only re-evaluation that creates a brand-new child
 * analysis - the parent analysis this hook is called against is never
 * itself mutated. On success, both the parent's findings (unchanged, but
 * worth a refetch for consistency) and the new child analysis's own cache
 * entry are seeded/invalidated so a caller can navigate straight to it. */
export function useCorrectField(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: FieldCorrectionRequest) =>
      api.post<FieldCorrectionResponse>(`/v1/analyses/${analysisId}/corrections`, payload),
    onSuccess: (response) => {
      queryClient.setQueryData(
        analysisQueryKey(response.child_analysis.id),
        response.child_analysis,
      );
      queryClient.invalidateQueries({ queryKey: findingsQueryKey(analysisId) });
    },
  });
}

/** The review queue (P6-T6): every `needs_review`/`review` analysis for the
 * org, oldest-waiting-first, each paired with `sla_since` - the moment it
 * first entered one of those states (`app.review.service.list_review_queue`,
 * derived from `AnalysisEvent` history, not a separate mutable timestamp). */
export const reviewQueueQueryKey = ["review", "queue"] as const;

export function useReviewQueue() {
  return useQuery({
    queryKey: reviewQueueQueryKey,
    queryFn: () => api.get<ReviewQueueEntryOut[]>("/v1/review/queue"),
  });
}

/** Sets or clears (`reviewer_id: null`) who is working an analysis. Only
 * meaningful while the analysis is non-terminal - see
 * `Analysis.assigned_reviewer_id`'s own docstring on the backend for why
 * this stays freely mutable there and freezes automatically once the
 * analysis completes. */
export function useAssignReviewer(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: AssignReviewerRequest) =>
      api.patch(`/v1/analyses/${analysisId}/assignment`, payload),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: analysisQueryKey(analysisId) });
      queryClient.invalidateQueries({ queryKey: reviewQueueQueryKey });
    },
  });
}

export const analysisSignoffQueryKey = (analysisId: string) =>
  ["analyses", analysisId, "signoff"] as const;

export function useAnalysisSignoff(analysisId: string | undefined) {
  return useQuery({
    queryKey: analysisSignoffQueryKey(analysisId ?? ""),
    queryFn: () => api.get<SignoffOut | null>(`/v1/analyses/${analysisId}/signoff`),
    enabled: Boolean(analysisId),
  });
}

/** Freezes review (IMPLEMENTATION.md §12's "sign-off action freezing the
 * review") - once this succeeds, the backend rejects any further finding
 * decision or field correction on this analysis with 409, and a generated
 * report becomes eligible to be marked final (`ReportOut.signed_off_by`). */
export function useSignOffAnalysis(analysisId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<SignoffOut>(`/v1/analyses/${analysisId}/signoff`),
    onSuccess: (signoff) => {
      queryClient.setQueryData(analysisSignoffQueryKey(analysisId), signoff);
      queryClient.invalidateQueries({ queryKey: reviewQueueQueryKey });
    },
  });
}
