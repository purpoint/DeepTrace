/**
 * Talking to the API.
 *
 * One place that knows the base URL, one place that reads the error envelope,
 * and one error type the rest of the application catches. A component that
 * calls fetch directly ends up with its own idea of what a failure looks like,
 * and the third one gets it wrong.
 *
 * The backend answers every failure with the same envelope carrying a
 * machine-readable code, so this reads the code rather than the message: a UI
 * that branches on message text breaks the first time the wording improves.
 */

import type {
  CancelResponse,
  ClaimView,
  Depth,
  EvidenceView,
  Health,
  ReportView,
  ResearchDetail,
  ResearchSummary,
  SourceView,
  SubmitResponse,
  TraceView,
} from "./types";

/** Everything is served under /api, which the dev server proxies to the
 *  backend. One origin in development means CORS is not silently load-bearing
 *  in a way that only breaks in production. */
const BASE = "/api";

export type ApiErrorCode =
  | "not_found"
  | "invalid_request"
  | "conflict"
  | "unavailable"
  | "internal"
  | "network";

export class ApiError extends Error {
  constructor(
    readonly code: ApiErrorCode,
    message: string,
    readonly status: number,
    readonly reference?: string,
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** Whether trying again could plausibly work. A 503 is worth a retry button;
   *  a 404 is worth a different page. */
  get isRetryable(): boolean {
    return this.code === "unavailable" || this.code === "network";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${BASE}${path}`, {
      ...init,
      headers: {
        "Content-Type": "application/json",
        ...(init?.headers ?? {}),
      },
    });
  } catch (cause) {
    // The request never reached the server: offline, DNS, the API not running.
    // Distinct from a server error, because the remedy is different and the
    // user can often see the cause themselves.
    throw new ApiError("network", "Could not reach the service.", 0, undefined);
  }

  if (!response.ok) {
    const body = await response.json().catch(() => null);
    const error = body?.error;
    throw new ApiError(
      error?.code ?? "internal",
      error?.message ?? `Request failed with status ${response.status}.`,
      response.status,
      error?.reference ?? undefined,
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  health: () => request<Health>("/health"),

  submit: (question: string, depth: Depth, maxTasks?: number) =>
    request<SubmitResponse>("/research", {
      method: "POST",
      body: JSON.stringify({
        question,
        depth,
        ...(maxTasks ? { max_tasks: maxTasks } : {}),
      }),
    }),

  list: (limit = 20) => request<ResearchSummary[]>(`/research?limit=${limit}`),
  detail: (id: string) => request<ResearchDetail>(`/research/${id}`),
  report: (id: string) => request<ReportView>(`/research/${id}/report`),
  claims: (id: string) => request<ClaimView[]>(`/research/${id}/claims`),
  evidence: (id: string) => request<EvidenceView[]>(`/research/${id}/evidence`),
  sources: (id: string) => request<SourceView[]>(`/research/${id}/sources`),
  trace: (id: string) => request<TraceView>(`/research/${id}/trace`),

  cancel: (id: string) =>
    request<CancelResponse>(`/research/${id}/cancel`, { method: "POST" }),
};

/** The WebSocket URL for a run's progress, resuming after a sequence number.
 *
 *  Built from the page's own origin so it follows the dev proxy and works
 *  unchanged behind TLS -- a hard-coded ws:// URL is the line that works
 *  locally and fails the moment the site is served over https. */
export function eventsUrl(researchId: string, after = 0): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${protocol}//${window.location.host}${BASE}/research/${researchId}/events?after=${after}`;
}
