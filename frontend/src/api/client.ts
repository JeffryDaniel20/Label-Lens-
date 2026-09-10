/**
 * Thin fetch wrapper for the LabelLens API.
 *
 * The backend authenticates via an httpOnly session cookie (never readable
 * from JS - see IMPLEMENTATION.md's auth decision), so every request must
 * send `credentials: "include"`. Mutating requests additionally need the
 * CSRF token the backend hands back once per session (in the login/signup
 * response body, and again on every `GET /v1/me` so a page reload can
 * recover it - see `app.identity.schemas.MeResponse.csrf_token` on the
 * backend) - `setCsrfToken`/`getCsrfToken` hold that value in memory only,
 * never in `localStorage`, so it never survives an XSS payload reading disk.
 *
 * Errors are parsed as RFC 9457 `application/problem+json`
 * (`app.platform.errors.problem_response` on the backend) into `ApiError`,
 * so callers get a real `status`/`title`/`detail` instead of a generic
 * "request failed."
 */

let csrfToken: string | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export class ApiError extends Error {
  readonly status: number;
  readonly type: string;
  readonly errors: { field: string; message: string }[];

  constructor(status: number, title: string, type: string, errors: ApiError["errors"] = []) {
    super(title);
    this.name = "ApiError";
    this.status = status;
    this.type = type;
    this.errors = errors;
  }
}

const SAFE_METHODS = new Set(["GET", "HEAD", "OPTIONS"]);

interface RequestOptions {
  method?: string;
  body?: unknown;
  signal?: AbortSignal;
}

export async function apiFetch<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? "GET";
  const headers: Record<string, string> = {};
  if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
  }
  if (!SAFE_METHODS.has(method) && csrfToken) {
    headers["X-CSRF-Token"] = csrfToken;
  }

  const response = await fetch(path, {
    method,
    headers,
    credentials: "include",
    signal: options.signal,
    body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
  });

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";
  const isJson = contentType.includes("json");
  const payload = isJson ? await response.json() : await response.text();

  if (!response.ok) {
    if (isJson && payload && typeof payload === "object") {
      throw new ApiError(
        response.status,
        String(payload.title ?? payload.detail ?? "Request failed"),
        String(payload.type ?? "unknown_error"),
        Array.isArray(payload.errors) ? payload.errors : [],
      );
    }
    throw new ApiError(response.status, "Request failed", "unknown_error");
  }

  return payload as T;
}

export const api = {
  get: <T>(path: string, signal?: AbortSignal) => apiFetch<T>(path, { method: "GET", signal }),
  post: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "POST", body }),
  patch: <T>(path: string, body?: unknown) => apiFetch<T>(path, { method: "PATCH", body }),
  delete: <T>(path: string) => apiFetch<T>(path, { method: "DELETE" }),
};
