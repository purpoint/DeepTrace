/** Everything that has been researched, newest first. */

import { Link } from "react-router-dom";

import { useHistory } from "../api/hooks";
import { SummaryCardTrigger } from "../components/SummaryCard";
import { Empty, Failure, Panel, Spinner, relativeTime } from "../components/ui";

const STATUS_COLOUR: Record<string, string> = {
  completed: "text-verdict-supported",
  failed: "text-verdict-unsupported",
  partial: "text-verdict-partial",
};

export function History() {
  const history = useHistory(50);

  return (
    <div className="mx-auto max-w-5xl animate-fade-up px-6 py-10">
      {/* "Every question that has been asked" was true when there was one
          history. It is now this account's history and nobody else's, and a
          subtitle that still claims otherwise is the interface making a
          promise the API stopped keeping. */}
      <Panel title="Your research" subtitle="Every question you have asked">
        {history.isLoading ? <Spinner label="Loading…" /> : null}
        {history.error ? <Failure message="Could not load the history." /> : null}
        {history.data?.length === 0 ? <Empty>You have not researched anything yet.</Empty> : null}

        <ul className="divide-y divide-line">
          {(history.data ?? []).map((run) => (
            <li key={run.research_id}>
              <Link
                to={`/research/${run.research_id}`}
                className="-mx-2 flex items-start justify-between gap-4 rounded-lg px-2 py-3 transition-colors hover:bg-raised"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm text-ink">{run.question}</p>
                  <p className="mt-1 text-xs text-faint">
                    {run.depth} · {relativeTime(run.created_at)}
                    {run.error ? ` · ${run.error.slice(0, 60)}` : ""}
                  </p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {/* Only where there is something to summarise. Offering it on
                      a failed run is offering a button that opens an apology. */}
                  {run.status === "completed" || run.status === "partial" ? (
                    <SummaryCardTrigger researchId={run.research_id} />
                  ) : null}
                  <span className={`text-xs ${STATUS_COLOUR[run.status] ?? "text-faint"}`}>
                    {run.status}
                  </span>
                </div>
              </Link>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
