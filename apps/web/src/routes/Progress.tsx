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
  const { events, state, finished } = useProgress(detail.research_id, { live: running });

  // When the stream says the run finished, the finished-run queries become
  // fetchable. Invalidating here rather than polling is what keeps the page
  // from asking for a report that does not exist yet.
  useEffect(() => {
    if (finished) void client.invalidateQueries({ queryKey: keys.detail(detail.research_id) });
  }, [finished, client, detail.research_id]);

  const reached = stageIndex(events);

  // And again whenever the run reaches a new stage.
  //
  // Without this the header badge reads "queued" for the entire run while the
  // panel below it narrates the work in real time, and the tab counts stay at
  // whatever they were when the page loaded. The detail query does not poll --
  // deliberately, because the socket is supposed to say when something
  // changed -- so if nothing invalidates it, nothing ever does.
  //
  // Keyed on the stage rather than on the event count: a run emits dozens of
  // events and refetching on each would put the page back to polling, only
  // less predictably. Seven refetches over several minutes is the actual cost.
  useEffect(() => {
    void client.invalidateQueries({ queryKey: keys.detail(detail.research_id) });
  }, [reached, client, detail.research_id]);

  return (
    <div className="space-y-4">
      <Panel
        title={running ? "Researching" : "How this run went"}
        subtitle={
          state === "unavailable"
            ? running
              ? "Live updates are unavailable; this page is polling instead."
              : "Live updates are unavailable, so the recording cannot be read back."
            : !running
              ? // A finished run is a replay, and saying "Live" over one is the
                // screen claiming to watch something that stopped days ago.
                state === "closed"
                ? "Replayed from what was recorded"
                : "Replaying…"
              : state === "closed" && !finished
                ? "Reconnecting…"
                : "Live"
        }
        actions={
          running ? (
            <button
              onClick={() => cancel.mutate()}
              disabled={cancel.isPending}
              className="rounded-lg border border-line px-2.5 py-1 text-xs text-muted transition-colors hover:border-verdict-unsupported/50 hover:text-verdict-unsupported"
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
                  className={`flex h-5 w-5 items-center justify-center rounded-full text-[11px] transition-colors ${
                    done
                      ? "bg-verdict-supported/15 text-verdict-supported"
                      : active
                        ? "animate-pulse-ring bg-brand text-canvas"
                        : "bg-raised text-faint"
                  }`}
                >
                  {done ? "✓" : index + 1}
                </span>
                <span className={done || active ? "text-ink" : "text-faint"}>
                  {stage.label}
                </span>
              </li>
            );
          })}
        </ol>
      </Panel>

      <Panel
        title={running ? "What it is doing" : "What it did"}
        subtitle={`${events.length} events`}
      >
        {events.length === 0 ? (
          running ? (
            <Spinner label="Waiting for the worker to pick this up…" />
          ) : state === "closed" || state === "unavailable" ? (
            // Not a failure, and not worth an error colour. Progress is kept in
            // a capped list per run, so an old run's narration is genuinely
            // gone -- and saying so is better than a spinner that waits for a
            // worker which finished with this run long ago.
            <p className="text-sm text-muted">
              This run's progress is no longer recorded. The step-by-step
              narration is kept for a while after a run and then dropped; the
              report, claims, evidence and trace are permanent.
            </p>
          ) : (
            <Spinner label="Reading back what was recorded…" />
          )
        ) : (
          <ul className="space-y-2.5 font-mono text-xs">
            {events.map((event) => (
              // Each event animates in as it arrives, which is what makes the
              // stream feel live rather than like a list that keeps redrawing.
              <li key={event.sequence} className="flex animate-slide-in gap-3">
                <span className="w-5 shrink-0 text-right text-faint tabular-nums">
                  {event.sequence}
                </span>
                <span
                  className={`w-40 shrink-0 ${
                    event.kind === "failed"
                      ? "text-verdict-unsupported"
                      : event.kind === "completed"
                        ? "text-verdict-supported"
                        : "text-brand/70"
                  }`}
                >
                  {event.kind}
                </span>
                {/* Rendered as text. The message can quote a page title, which
                    came from a site we do not control. */}
                <span className="text-muted">{event.message}</span>
              </li>
            ))}
          </ul>
        )}
      </Panel>
    </div>
  );
}
