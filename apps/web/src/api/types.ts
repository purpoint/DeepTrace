/**
 * The shapes the API sends.
 *
 * Hand-written against the OpenAPI schema rather than generated, because the
 * generated version of a small surface is harder to read than the surface, and
 * this file is where a reader learns what the system produces. The trade is
 * real: these can drift, so the contract tests on the backend are what keep
 * them honest, not this file's good intentions.
 *
 * Fields marked UNTRUSTED originate on pages the system does not control. They
 * reach the DOM in this application, and every one of them is rendered as text
 * or through the sanitizing markdown renderer -- never as HTML.
 */

export type Depth = "quick" | "standard" | "deep";

export type ClaimStatus =
  | "proposed"
  | "supported"
  | "partially_supported"
  | "unsupported"
  | "conflicting";

export interface SubmitResponse {
  job_id: string;
  research_id: string;
  status: string;
  poll: string;
}

export interface ResearchSummary {
  research_id: string;
  question: string; // written by the user
  depth: Depth;
  status: string;
  created_at: string;
  completed_at: string | null;
  error: string | null;
}

export interface JobView {
  job_id: string;
  status: string;
  attempts: number;
  worker: string | null;
  error: string | null;
}

export interface ResearchDetail extends ResearchSummary {
  normalized_question: string | null;
  /** What the analyzer decided this question was. Null until it has been
   *  analysed -- a run polled the moment it was submitted has no type yet. */
  research_type: string | null;
  sources: number;
  evidence: number;
  claims: number;
  has_report: boolean;
  job: JobView | null;
}

export interface SourceView {
  id: string;
  url: string;
  title: string; // UNTRUSTED
  domain: string; // UNTRUSTED
  source_type: string;
  quality_score: number;
  word_count: number;
  fetch_failed: boolean;
  retrieved_at: string;
}

export interface EvidenceView {
  id: string;
  source_id: string;
  task_id: string | null;
  claim: string; // UNTRUSTED
  supporting_text: string; // UNTRUSTED
  location: string; // UNTRUSTED
  quote_status: "verbatim" | "normalised" | "paraphrased" | "not_found" | string;
  quote_similarity: number;
  weight: number;
}

export interface ClaimView {
  id: string;
  text: string; // UNTRUSTED
  kind: string;
  status: ClaimStatus;
  confidence: string;
  strength: number;
  condition: string | null;
  disposition: string | null;
  reasoning: string | null;
  overgeneralization: string | null;
  suggested_revision: string | null;
  follow_up_question: string | null;
  conflicts_with: string[];
  contradicted_by: string[];
}

export interface Citation {
  number: number;
  evidence_id: string;
  source_id: string;
  url: string;
  title: string; // UNTRUSTED
  domain: string; // UNTRUSTED
  location: string;
  quote: string; // UNTRUSTED
  quote_status: string;
  claim_ids: string[];
}

export interface ReportView {
  research_id: string;
  title: string; // UNTRUSTED
  markdown: string; // UNTRUSTED
  structured: {
    title: string;
    sections: { kind: string; body: string; citation_numbers: number[] }[];
    citations: Citation[];
    unresolved_markers: string[];
  };
  citations: number;
  fully_cited: boolean;
}

export interface TraceEntry {
  kind: "model" | "tool";
  name: string;
  started_at: string;
  latency_ms: number;
  status: string;
  detail: Record<string, unknown>;
}

export interface TraceView {
  research_id: string;
  entries: TraceEntry[];
  total_tokens: number;
  /** Null when any call had unknown pricing. Not zero -- unmeasured and free
   *  are different facts, and the UI must not report the first as the second. */
  cost_usd: number | null;
}

export interface CancelResponse {
  research_id: string;
  cancelled: boolean;
  message: string;
}

export interface Health {
  status: string;
  database: boolean;
  queue: boolean;
  version: string;
}

export type EventKind =
  | "queued"
  | "started"
  | "stage"
  | "task_completed"
  | "sources_found"
  | "evidence_extracted"
  | "claims_verified"
  | "report_ready"
  | "completed"
  | "failed"
  | "cancelled";

export interface ProgressEvent {
  version: number;
  sequence: number;
  research_id: string;
  kind: EventKind;
  message: string;
  data: Record<string, unknown>;
  at: string;
}

export const TERMINAL_EVENTS: ReadonlySet<string> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

/** The signed-in account, as its owner sees it.
 *
 *  There is no password field here and there is none on the wire either. The
 *  API's response model has nowhere to put one, which is the point of it
 *  having a response model separate from its database row. */
export interface Account {
  id: string;
  email: string;
  display_name: string | null;
  created_at: string;
  last_login_at: string | null;
}

/** What one agent spent on a run.
 *
 *  `cost_usd` is null when any call in the group had no recorded price. The API
 *  reports no cost rather than a partial sum, because a sum over unpriced calls
 *  looks authoritative and understates. */
export interface AgentSpend {
  agent: string;
  model: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  latency_ms: number;
  cost_usd: number | null;
  unpriced: number;
  failed: number;
}

/** What one tool cost in time. Tools have no token cost, and on a rate-limited
 *  provider the wall clock is dominated by waiting rather than spending. */
export interface ToolSpend {
  tool: string;
  calls: number;
  latency_ms: number;
  failed: number;
}

export interface CostView {
  research_id: string;
  total_cost_usd: number | null;
  /** Whether every model call in this run had a recorded price. A client that
   *  shows a figure without reading this is showing a number the API did not
   *  stand behind. */
  complete: boolean;
  unpriced_calls: number;
  total_input_tokens: number;
  total_output_tokens: number;
  model_latency_ms: number;
  tool_latency_ms: number;
  by_agent: AgentSpend[];
  by_tool: ToolSpend[];
  /** Models whose recorded price is past its published end date. */
  stale_prices: string[];
}
