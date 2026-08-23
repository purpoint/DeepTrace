/**
 * Watching a run happen.
 *
 * The screen that justifies the WebSocket. A run takes minutes and produces
 * nothing until it finishes, so without this the user's experience is a spinner
 * and a guess about whether anything is happening -- and the usual response to
 * that is to submit the question again, which costs real money.
 *
 * Every event is shown as it arrives, including the ones that report a
 * disappointment: a task that found nothing, passages that were rejected. A
 * progress view that only shows success is a progress view that surprises
 * people at the end.
 */

import { useEffect } from "react";
import { useQueryClient } from "@tanstack/react-query";

import { keys, useCancel } from "../api/hooks";
import { useProgress } from "../api/useProgress";
import type { ProgressEvent, ResearchDetail } from "../api/types";
import { Panel, Spinner } from "../components/ui";

const STAGES = [
  { key: "analysing", label: "Understanding the question" },
  { key: "planning", label: "Planning the research" },
  { key: "researching", label: "Searching and reading" },
  { key: "extracting", label: "Extracting and verifying passages" },
  { key: "analysing-evidence", label: "Analysing the evidence" },
  { key: "verifying", label: "Checking every claim" },
  { key: "reporting", label: "Writing the report" },
];

/** Which of the fixed stages an event corresponds to.
 *
 *  Derived from the event kind rather than from a stage name in the payload,
 *  so the backend can rename an internal phase without the progress bar
 *  silently stopping halfway. */
function stageIndex(events: ProgressEvent[]): number {
  let index = 0;
  for (const event of events) {
    if (event.kind === "started") index = Math.max(index, 0);
    if (event.kind === "stage" && String(event.data.stage) === "planning") index = 1;
    if (event.kind === "stage" && String(event.data.stage) === "researching") index = 2;
    if (event.kind === "task_completed") index = Math.max(index, 2);
    if (event.kind === "evidence_extracted") index = 4;
    if (event.kind === "stage" && String(event.data.stage) === "verifying") index = 5;
    if (event.kind === "claims_verified") index = 6;
    if (event.kind === "report_ready") index = STAGES.length;
  }
  return index;
}

export function ProgressView({ detail }: { detail: ResearchDetail }) {
  const client = useQueryClient();
  const cancel = useCancel(detail.research_id);
  const running = !["completed", "failed", "cancelled", "partial"].includes(detail.status);
  const { events, state, finished } = useProgress(detail.research_id, running);

  // When the stream says the run finished, the finished-run queries become
  // fetchable. Invalidating here rather than polling is what keeps the page
  // from asking for a report that does not exist yet.
  useEffect(() => {
    if (finished) void client.invalidateQueries({ queryKey: keys.detail(detail.research_id) });
  }, [finished, client, detail.research_id]);

  const reached = stageIndex(events);

  return (
    <div className="space-y-4">
      <Panel
        title="Researching"
        subtitle={
          state === "unavailable"
            ? "Live updates are unavailable; this page is polling instead."
            : state === "closed" && !finished
              ? "Reconnecting…"
              : "Live"
        }
        actions={
          running ? (
            <button
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              className="rounded border border-slate-300 px-2 py-1 text-xs text-slate-700 hover:bg-slate-50"
            >
              {cancel.isPending ? "Stopping…" : "Stop"}
            </button>
          ) : null
        }
      >
        <ol className="space-y-2">
          {STAGES.map((stage, index) => {
            const done = index < reached;
            const active = index === reached && running;
            return (
              <li key={stage.key} className="flex items-center gap-3 text-sm">
                <span
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[11px] ${
                    done
                      ? "bg-green-600 text-white"
                      : active
                        ? "bg-slate-900 text-white"
                        : "bg-slate-200 text-slate-500"
                  }`}
                >
                  {done ? "✓" : index + 1}
                </span>
                <span className={done || active ? "text-slate-900" : "text-slate-400"}>
                  {stage.label}
                </span>
                {active ? (
                  <span className="h-2 w-2 animate-pulse rounded-full bg-slate-900" />
                ) : null}
              </li>
            );
          })}
        </ol>
      </Panel>

      <Panel title="What it is doing" subtitle={`${events.length} events`}>
        {events.length === 0 ? (
          <Spinner label="Waiting for the worker to pick this up…" />
        ) : (
          <ul className="space-y-2 font-mono text-xs">
            {events.map((event) => (
              <li key={event.sequence} className="flex gap-3">
                <span className="w-6 shrink-0 text-right text-slate-400">
                  {event.sequence}
                </span>
                <span
                  className={`w-40 shrink-0 ${
                    event.kind === "failed" ? "text-red-700" : "text-slate-500"
                  }`}
                >
                  {event.kind}
                </span>
                {/* Rendered as text. The message can quote a page title, which
                    came from a site we do not control. */}
                <span className="text-slate-800">{event.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
