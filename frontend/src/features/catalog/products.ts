import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api } from "@/api/client";
import type { ProductCreateRequest, ProductOut, ProductUpdateRequest } from "@/api/types";

export const PRODUCTS_QUERY_KEY = ["products"] as const;
export const productQueryKey = (productId: string) => ["products", productId] as const;

export function useProducts() {
  return useQuery({
    queryKey: PRODUCTS_QUERY_KEY,
    queryFn: () => api.get<ProductOut[]>("/v1/products"),
  });
}

export function useProduct(productId: string | undefined) {
  return useQuery({
    queryKey: productQueryKey(productId ?? ""),
    queryFn: () => api.get<ProductOut>(`/v1/products/${productId}`),
    enabled: Boolean(productId),
  });
}

export function useCreateProduct() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProductCreateRequest) => api.post<ProductOut>("/v1/products", payload),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PRODUCTS_QUERY_KEY }),
  });
}

export function useUpdateProduct(productId: string) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: ProductUpdateRequest) =>
      api.patch<ProductOut>(`/v1/products/${productId}`, payload),
    onSuccess: (product) => {
      queryClient.setQueryData(productQueryKey(productId), product);
      return queryClient.invalidateQueries({ queryKey: PRODUCTS_QUERY_KEY });
    },
  });
}
