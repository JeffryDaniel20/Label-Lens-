import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiError, setCsrfToken } from "@/api/client";
import type { LoginRequest, LoginResponse, MeResponse, SignupRequest } from "@/api/types";

export const SESSION_QUERY_KEY = ["session", "me"] as const;

/** `GET /v1/me` doubles as "am I logged in" - a 401 means no session, which
 * is an expected, non-error outcome here (not every visitor is signed in),
 * so it resolves to `null` rather than rejecting the query. */
async function fetchSession(): Promise<MeResponse | null> {
  try {
    const me = await api.get<MeResponse>("/v1/me");
    // A page reload has the session cookie but not the in-memory CSRF
    // token - `/v1/me` is what lets it recover without re-authenticating.
    setCsrfToken(me.csrf_token ?? null);
    return me;
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      return null;
    }
    throw error;
  }
}

export function useSession() {
  return useQuery({
    queryKey: SESSION_QUERY_KEY,
    queryFn: fetchSession,
    staleTime: 60_000,
    retry: false,
  });
}

export function useLogin() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: LoginRequest) => api.post<LoginResponse>("/v1/auth/login", payload),
    onSuccess: (data) => {
      setCsrfToken(data.csrf_token);
      return queryClient.invalidateQueries({ queryKey: SESSION_QUERY_KEY });
    },
  });
}

export function useSignup() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: SignupRequest) => api.post<LoginResponse>("/v1/auth/signup", payload),
    onSuccess: (data) => {
      setCsrfToken(data.csrf_token);
      return queryClient.invalidateQueries({ queryKey: SESSION_QUERY_KEY });
    },
  });
}

export function useLogout() {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: () => api.post<void>("/v1/auth/logout"),
    onSuccess: () => {
      setCsrfToken(null);
      queryClient.setQueryData(SESSION_QUERY_KEY, null);
    },
  });
}
