/**
 * Thin fetch wrapper. Always hits same-origin /v1 (Next rewrites proxy to
 * the FastAPI dev server on 8080).
 */
export class ApiError extends Error {
  constructor(
    public readonly status: number,
    public readonly detail: string,
  ) {
    super(`ApiError ${status}: ${detail}`);
  }
}

async function req<T>(
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const res = await fetch(path, {
    ...init,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(init.headers ?? {}),
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body?.detail ?? detail;
    } catch {}
    throw new ApiError(res.status, String(detail));
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  get: <T>(path: string) => req<T>(path, { method: "GET" }),
  post: <T>(path: string, body: unknown, headers: Record<string, string> = {}) =>
    req<T>(path, {
      method: "POST",
      body: JSON.stringify(body ?? {}),
      headers,
    }),
  del: <T>(path: string) => req<T>(path, { method: "DELETE" }),
};
