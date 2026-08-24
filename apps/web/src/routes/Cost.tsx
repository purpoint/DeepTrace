/**
 * What a run cost, and how much of that figure can be trusted.
 *
 * The trace already carries a total, and a total is the least useful form of
 * the answer: "this run cost eleven cents" only prompts "on what". So this
 * breaks it down by agent, which is the version somebody can act on -- the
 * planner and the reporter run on the strong tier, and if a run is expensive it
 * is almost always one of them.
 *
 * Two things this screen refuses to do.
 *
 * It never shows an unmeasured cost as zero. A run whose model prices are not
 * recorded and a run that was free are different facts, and rendering the first
 * as "$0.00" is a lie that looks like good news.
 *
 * It never shows a partial sum as a total. A group containing one unpriced call
 * reports no cost at all rather than the sum of the calls it could price, which
 * would look authoritative and understate.
 *
 * Tool time is shown beside model time because on a rate-limited provider the
 * wall clock is dominated by waiting rather than spending, and a cost view that
 * shows only tokens explains the invoice but not the nine minutes.
 */

import { useCost, useResearch } from "../api/hooks";
import type { AgentSpend, ToolSpend } from "../api/types";
import { Empty, Failure, Panel, Spinner } from "../components/ui";

function money(value: number | null): string {
  if (value === null) return "not measured";
  // Four decimals: a research run costs cents, and two decimals would render
  // every honest figure as $0.00.
  return `$${value.toFixed(4)}`;
}

function seconds(ms: number): string {
  return `${(ms / 1000).toFixed(1)}s`;
}

/** Turn an internal identifier into something written for a person.
 *
 *  `fact_checker` and `query_analyzer` are the names the system calls itself.
 *  On the trace screen that is right -- those are the rows it wrote while it
 *  worked. Here the heading is "where the money went", and a snake_case
 *  identifier in a sentence like that is the same defect as the raw enum value
 *  that once reached a finished report: nothing catches it except reading the
 *  page. */
function label(name: string): string {
  return name.replace(/_/g, " ").replace(/^./, (first) => first.toUpperCase());
}

function AgentRow({ row, widest }: { row: AgentSpend; widest: number }) {
  const share = widest > 0 && row.cost_usd !== null ? (row.cost_usd / widest) * 100 : 0;

  return (
    <li className="py-2.5">
      <div className="flex items-baseline justify-between gap-3 text-sm">
        <span className="font-medium text-ink">{label(row.agent)}</span>
        <span className="font-mono text-xs tabular-nums text-muted">
          {money(row.cost_usd)}
        </span>
      </div>

      {/* A bar rather than only a number: the point of a breakdown is which row
          is the large one, and that is a comparison the eye makes faster than
          arithmetic. Omitted when the cost is unmeasured, because a zero-width
          bar reads as "cheap" rather than "unknown". */}
      {row.cost_usd !== null ? (
        <div className="mt-1.5 h-1 overflow-hidden rounded-full bg-raised">
          <div className="h-full rounded-full bg-brand/70" style={{ width: `${share}%` }} />
        </div>
      ) : null}

      <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5 font-mono text-[11px] text-faint">
        <span>{row.model}</span>
        <span>{row.calls} {row.calls === 1 ? "call" : "calls"}</span>
        <span>{row.input_tokens.toLocaleString()} in</span>
        <span>{row.output_tokens.toLocaleString()} out</span>
        <span>{seconds(row.latency_ms)}</span>
        {row.unpriced > 0 ? (
          <span className="text-verdict-partial">{row.unpriced} unpriced</span>
        ) : null}
        {row.failed > 0 ? (
          <span className="text-verdict-unsupported">{row.failed} failed</span>
        ) : null}
      </div>
    </li>
  );
}

function ToolRow({ row }: { row: ToolSpend }) {
  return (
    <li className="flex items-baseline justify-between gap-3 py-2 text-sm">
      <span className="font-medium text-ink">{label(row.tool)}</span>
      <span className="font-mono text-xs tabular-nums text-muted">
        {/* Spelled out rather than "14 × 31.8s", which reads as a
            multiplication when the second number is already the total. */}
        {row.calls} {row.calls === 1 ? "call" : "calls"} · {seconds(row.latency_ms)} total
        {row.failed > 0 ? (
          <span className="ml-2 text-verdict-unsupported">{row.failed} failed</span>
        ) : null}
      </span>
    </li>
  );
}

export function Cost({ researchId, ready }: { researchId: string; ready: boolean }) {
  const detail = useResearch(researchId, { live: false });
  const cost = useCost(researchId, ready);

  if (!ready) {
    return <Empty>The bill is available once the run has finished.</Empty>;
  }
  if (cost.isLoading) return <Spinner label="Adding it up" />;
  if (cost.error) {
    return <Failure message="Could not load what this run cost." onRetry={() => cost.refetch()} />;
  }
  if (!cost.data) return null;

  const data = cost.data;
  const widest = Math.max(0, ...data.by_agent.map((row) => row.cost_usd ?? 0));

  return (
    <div className="space-y-5">
      <Panel
        title="What this run cost"
        subtitle={
          data.complete
            ? "Every model call had a recorded price."
            : `${data.unpriced_calls} call${data.unpriced_calls === 1 ? "" : "s"} had no recorded price, so there is no total.`
        }
      >
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-4">
          {[
            ["Cost", money(data.total_cost_usd)],
            ["Input tokens", data.total_input_tokens.toLocaleString()],
            ["Output tokens", data.total_output_tokens.toLocaleString()],
            ["Model time", seconds(data.model_latency_ms)],
          ].map(([label, value]) => (
            <div key={label}>
              <div className="text-xs text-faint">{label}</div>
              <div className="mt-0.5 font-mono text-sm tabular-nums text-ink">{value}</div>
            </div>
          ))}
        </div>

        {/* Said on the screen rather than left in a docstring. A price past its
            published end date still computes; it is simply no longer a number
            anyone should quote. */}
        {data.stale_prices.length > 0 ? (
          <p className="mt-4 rounded-xl bg-verdict-partial/10 px-3.5 py-2.5 text-sm text-verdict-partial ring-1 ring-inset ring-verdict-partial/25">
            Priced using rates that are past their published end date (
            {data.stale_prices.join(", ")}). The arithmetic is right; the rates
            are stale. Re-verify before quoting these figures.
          </p>
        ) : null}
      </Panel>

      <Panel title="By agent" subtitle="Where the money went">
        {data.by_agent.length === 0 ? (
          <Empty>No model calls were recorded for this run.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {data.by_agent.map((row) => (
              <AgentRow key={`${row.agent}:${row.model}`} row={row} widest={widest} />
            ))}
          </ul>
        )}
      </Panel>

      <Panel
        title="By tool"
        subtitle={`Where the time went — ${seconds(data.tool_latency_ms)} outside the models`}
      >
        {data.by_tool.length === 0 ? (
          <Empty>No tool calls were recorded for this run.</Empty>
        ) : (
          <ul className="divide-y divide-line">
            {data.by_tool.map((row) => (
              <ToolRow key={row.tool} row={row} />
            ))}
          </ul>
        )}
      </Panel>

      {detail.data?.status === "partial" ? (
        <p className="text-xs text-faint">
          This run did not finish, so these figures cover only what it managed to do.
        </p>
      ) : null}
    </div>
  );
}
