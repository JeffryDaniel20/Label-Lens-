import { useQuery } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { VersionComparisonOut } from "@/api/types";

export const versionComparisonQueryKey = (
  fromVersionId: string,
  toVersionId: string,
  commonRuleset: boolean,
) => ["product-versions", fromVersionId, "compare", toVersionId, commonRuleset] as const;

/** P6-T7: field + finding diff between two versions of the same product.
 * `commonRuleset` toggles the backend's "compare under common ruleset"
 * recompute (`app.analysis.comparison`) - re-evaluating both sides against
 * the same rule content so a finding difference can no longer be
 * attributed to the rules themselves having changed. */
export function useVersionComparison(
  fromVersionId: string | undefined,
  toVersionId: string | undefined,
  commonRuleset: boolean,
) {
  return useQuery({
    queryKey: versionComparisonQueryKey(fromVersionId ?? "", toVersionId ?? "", commonRuleset),
    queryFn: () =>
      api.get<VersionComparisonOut>(
        `/v1/product-versions/${fromVersionId}/compare/${toVersionId}?common_ruleset=${commonRuleset}`,
      ),
    enabled: Boolean(fromVersionId && toVersionId),
  });
}
