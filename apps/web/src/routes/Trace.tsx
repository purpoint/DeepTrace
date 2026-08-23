/**
 * Every model call and every tool call, in the order they happened.
 *
 * The screen the project is named for. A reader who does not trust the report
 * can read what produced it: which prompts ran, which searches were issued,
 * what each cost, and what failed. Nothing here is a summary -- these are the
 * rows the system wrote while it worked.
 *
 * Cost is shown as "not measured" when the API returns null, never as zero. A
 * run whose model prices are unknown and a run that cost nothing are different
 * facts, and the second one would be a lie.
 */

import { useResearch, useTrace } from "../api/hooks";
import type { TraceEntry } from "../api/types";
import { Empty, Failure, Panel, Spinner } from "../components/ui";

function Entry({ entry, offsetMs }: { entry: TraceEntry; offsetMs: number }) {
  const detail = entry.detail;
  const failed = entry.status !== "success";

  return (
    <li className="flex gap-3 py-2 font-mono text-xs">
      <span className="w-16 shrink-0 text-right text-slate-400 tabular-nums">
        +{(offsetMs / 1000).toFixed(1)}s
      </span>
      <span
        className={`w-12 shrink-0 ${
          entry.kind === "model" ? "text-violet-700" : "text-teal-700"
        }`}
      >
        {entry.kind}
      </span>
      <span className="w-40 shrink-0 truncate text-slate-800">{entry.name}</span>
      <span className="w-16 shrink-0 text-right text-slate-500 tabular-nums">
        {entry.latency_ms.toFixed(0)}ms
      </span>
      <span className={`flex-1 truncate ${failed ? "text-red-700" : "text-slate-500"}`}>
        {entry.kind === "model"
          ? `${detail.model ?? ""} · ${detail.input_tokens ?? 0} in / ${detail.output_tokens ?? 0} out`
          : `${detail.task_id ?? ""} ${detail.result_count != null ? `· ${detail.result_count} results` : ""}`}
        {failed ? ` · ${entry.status}` : ""}
      </span>
    </li>
  );
}

export function Trace({ researchId, ready }: { researchId: string; ready: boolean }) {
  const trace = useTrace(researchId, ready);
  const detail = useResearch(researchId, { live: false });

  if (!ready) return <Panel title="Trace"><Empty>Nothing has run yet.</Empty></Panel>;
  if (trace.isLoading) return <Spinner label="Loading the trace…" />;
  if (trace.error) return <Failure message="Could not load the trace." />;

  const entries = trace.data?.entries ?? [];
  const start = entries.length ? new Date(entries[0]!.started_at).getTime() : 0;
  const models = entries.filter((entry) => entry.kind === "model").length;
  const tools = entries.length - models;

  return (
    <div className="space-y-4">
      <Panel title="How the answer was reached" subtitle="The chain, end to end">
        <ol className="space-y-2 text-sm">
          {[
            ["Question", detail.data?.question ?? ""],
            ["Interpreted as", detail.data?.normalized_question ?? "—"],
            ["Sources retrieved", `${detail.data?.sources ?? 0} pages`],
            ["Passages verified", `${detail.data?.evidence ?? 0} checked against their page`],
            ["Claims checked", `${detail.data?.claims ?? 0} against the evidence`],
            ["Report", detail.data?.has_report ? "written from verified claims" : "not written"],
          ].map(([label, value]) => (
            <li key={label} className="flex gap-3">
              <span className="w-40 shrink-0 text-slate-500">{label}</span>
              <span className="text-slate-900">{value}</span>
            </li>
          ))}
        </ol>
      </Panel>

      <Panel
        title="Calls"
        subtitle={`${models} model calls, ${tools} tool calls, ${trace.data?.total_tokens.toLocaleString() ?? 0} tokens`}
        actions={
          <span className="text-xs text-slate-500">
            {trace.data?.cost_usd == null
              ? "cost not measured"
              : `$${trace.data.cost_usd.toFixed(4)}`}
          </span>
        }
      >
        {entries.length === 0 ? (
          <Empty>No calls were recorded.</Empty>
        ) : (
          <ol className="divide-y divide-slate-50">
            {entries.map((entry, index) => (
              <Entry
                key={`${entry.started_at}-${index}`}
                entry={entry}
                offsetMs={new Date(entry.started_at).getTime() - start}
              />
            ))}
          </ol>
        )}
      </Panel>
    </div>
  );
}
