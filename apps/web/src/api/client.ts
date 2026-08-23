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
 *
 * It is also where credentials are attached. Every request carries the access
 * token, and a request refused because that token expired is retried once with
 * a fresh one -- so a fifteen-minute token is invisible to the rest of the
 * application. Only `token_expired` triggers that. Retrying on a plain
 * `unauthenticated` would mean a genuinely rejected credential is presented
 * twice, which is how a client turns one failed sign-in into a rate limit.
 */

import {
  accessTokenNow,
  endSession,
  refreshSession,
  type Tokens,
} from "./session";
import type {
  Account,
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
  | "network"
  | "unauthenticated"
  | "token_expired"
  | "rate_limited";

/** The `details` object the error envelope carries. Shapes vary by code; the
 *  only one read here is the field list a validation failure comes with. */
export interface ErrorDetails {
  fields?: { loc: (string | number)[]; msg: string }[];
  retry_after?: number;
  limit?: number;
}

export class ApiError extends Error {
  constructor(
    readonly code: ApiErrorCode,
    message: string,
    readonly status: number,
    readonly reference?: string,
    readonly details: ErrorDetails = {},
  ) {
    super(message);
    this.name = "ApiError";
  }

  /** The first field-level reason, if the failure came with one.
   *
   *  A validation failure's envelope message is deliberately generic -- "the
   *  request body is not valid" -- because it is written for every endpoint at
   *  once. The specific reason is in `details.fields`, and leaving it there
   *  means a person who mistypes an email is told only that something, somewhere,
   *  was wrong. */
  get fieldReason(): string | null {
    const first = this.details.fields?.[0];
    if (!first) return null;
    // Pydantic's messages start lowercase and read as fragments, so they are
    // capitalised into a sentence -- and only given a full stop if they do not
    // already end in one. Appending unconditionally produced "…with email..",
    // which no test noticed and one glance at the page did.
    const text = first.msg.charAt(0).toUpperCase() + first.msg.slice(1);
    return /[.!?]$/.test(text) ? text : `${text}.`;
  }

  /** Whether trying again could plausibly work. A 503 is worth a retry button;
   *  a 404 is worth a different page. */
  get isRetryable(): boolean {
    return this.code === "unavailable" || this.code === "network";
  }

  /** Whether the remedy is to sign in, rather than to try again. */
  get needsSignIn(): boolean {
    return this.code === "unauthenticated" || this.code === "token_expired";
  }
}

async function send(path: string, init: RequestInit | undefined, token: string | null) {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  if (token) headers.Authorization = `Bearer ${token}`;

  try {
    return await fetch(`${BASE}${path}`, { ...init, headers });
  } catch {
    // The request never reached the server: offline, DNS, the API not running.
    // Distinct from a server error, because the remedy is different and the
    // user can often see the cause themselves.
    throw new ApiError("network", "Could not reach the service.", 0, undefined);
  }
}

async function failure(response: Response): Promise<ApiError> {
  const body = await response.json().catch(() => null);
  const error = body?.error;
  return new ApiError(
    error?.code ?? "internal",
    error?.message ?? `Request failed with status ${response.status}.`,
    response.status,
    error?.reference ?? undefined,
    error?.details ?? {},
  );
}

async function request<T>(
  path: string,
  init?: RequestInit,
  options: { anonymous?: boolean } = {},
): Promise<T> {
  let response = await send(path, init, options.anonymous ? null : accessTokenNow());

  if (response.status === 401 && !options.anonymous) {
    const error = await failure(response.clone());

    // Only an expired token is worth a second attempt. A token that was
    // rejected for any other reason will be rejected again, and presenting it
    // twice is how a client spends its own rate limit.
    if (error.code === "token_expired" || accessTokenNow() === null) {
      const renewed = await refreshSession();
      if (renewed === null) {
        endSession();
        throw error;
      }
      response = await send(path, init, renewed);
    } else {
      endSession();
      throw error;
    }
  }

  if (!response.ok) throw await failure(response);

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export const api = {
  // The one endpoint with nothing to protect. Sending a token would mean the
  // sign-in screen cannot tell the user the service is down.
  health: () => request<Health>("/health", undefined, { anonymous: true }),

  register: (email: string, password: string) =>
    request<Tokens>(
      "/auth/register",
      { method: "POST", body: JSON.stringify({ email, password }) },
      { anonymous: true },
    ),

  login: (email: string, password: string) =>
    request<Tokens>(
      "/auth/login",
      { method: "POST", body: JSON.stringify({ email, password }) },
      { anonymous: true },
    ),

  me: () => request<Account>("/auth/me"),

  logout: (refreshToken: string) =>
    request<void>("/auth/logout", {
      method: "POST",
      body: JSON.stringify({ refresh_token: refreshToken }),
    }),

  /** A single-use credential for opening a progress stream. See eventsUrl. */
  wsTicket: () => request<{ ticket: string; expires_in: number }>("/auth/ws-ticket", {
    method: "POST",
  }),

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
 *  locally and fails the moment the site is served over https.
 *
 *  The ticket goes in the query string because a browser cannot set a header
 *  when opening a WebSocket. That is why it is a ticket and not the access
 *  token: it is good for thirty seconds and for one connection, so the copy
 *  that ends up in an access log is worthless by the time anyone reads it. */
export function eventsUrl(researchId: string, ticket: string, after = 0): string {
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const query = new URLSearchParams({ ticket, after: String(after) });
  return `${protocol}//${window.location.host}${BASE}/research/${researchId}/events?${query}`;
}
