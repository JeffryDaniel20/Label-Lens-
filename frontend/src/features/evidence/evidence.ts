import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { EvidenceDetailOut } from "@/api/types";

export const findingEvidenceQueryKey = (findingId: string) =>
  ["findings", findingId, "evidence"] as const;

/** `GET /v1/findings/{id}/evidence` (P5-T4's own literal acceptance
 * criterion endpoint) - real page id/no, real bbox in the page's native
 * pixel space, a real presigned page-image URL. Fetched lazily per finding
 * (only once a finding is actually selected in the viewer), not for every
 * finding up front - most findings in a real findings list are never
 * clicked in a given session. */
export function useFindingEvidence(findingId: string | undefined) {
  return useQuery({
    queryKey: findingEvidenceQueryKey(findingId ?? ""),
    queryFn: () => api.get<EvidenceDetailOut[]>(`/v1/findings/${findingId}/evidence`),
    enabled: Boolean(findingId),
  });
}
