import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { ProductVersionOut, VersionCreateRequest, VersionUpdateRequest } from "@/api/types";

export const versionsQueryKey = (productId: string) => ["products", productId, "versions"] as const;
export const versionQueryKey = (versionId: string) => ["product-versions", versionId] as const;

export function useProductVersions(productId: string | undefined) {
  return useQuery({
    queryKey: versionsQueryKey(productId ?? ""),
    queryFn: () => api.get<ProductVersionOut[]>(`/v1/products/${productId}/versions`),
    enabled: Boolean(productId),
  });
}

export function useProductVersion(versionId: string | undefined) {
  return useQuery({
    queryKey: versionQueryKey(versionId ?? ""),
    queryFn: () => api.get<ProductVersionOut>(`/v1/product-versions/${versionId}`),
    enabled: Boolean(versionId),
  });
}

export function useCreateVersion(productId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: VersionCreateRequest) =>
      api.post<ProductVersionOut>(`/v1/products/${productId}/versions`, payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: versionsQueryKey(productId) }),
  });
}

/** Backend rejects this with 409 once an analysis has locked the version -
 * see `app.catalog.service` - callers should surface `error.message` as-is,
 * it already reads as "create a new version instead." */
export function useUpdateVersion(versionId: string, productId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: VersionUpdateRequest) =>
      api.patch<ProductVersionOut>(`/v1/product-versions/${versionId}`, payload),
    onSuccess: (version) => {
      queryClient.setQueryData(versionQueryKey(versionId), version);
      return queryClient.invalidateQueries({ queryKey: versionsQueryKey(productId) });
    },
  });
}
