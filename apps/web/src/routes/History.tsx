/** Everything that has been researched, newest first. */

import { Link } from "react-router-dom";

import { useHistory } from "../api/hooks";
import { SummaryCardTrigger } from "../components/SummaryCard";
import { Logo } from "../components/Logo";
import { Failure, Panel, Spinner, relativeTime } from "../components/ui";

const STATUS_COLOUR: Record<string, string> = {
  completed: "text-verdict-supported",
  failed: "text-verdict-unsupported",
  partial: "text-verdict-partial",
};

export function History() {
  const history = useHistory(50);

  // The empty case gets its own screen rather than a line of grey text inside
  // a panel. A first visit here is a page reporting an absence and offering
  // nothing to do about it -- which wastes the one moment the reader has
  // nothing else competing for their attention. It says what will appear here
  // and points at the one action that makes it appear.
  if (history.data?.length === 0) {
    return (
      <div className="mx-auto flex min-h-[calc(100vh-4rem)] max-w-5xl flex-col justify-center px-6 py-10">
        <div className="mx-auto max-w-sm animate-fade-up text-center">
          <Logo className="mx-auto h-8 w-8 opacity-60" />
          <h1 className="mt-5 text-xl font-semibold tracking-tight text-ink">
            No research yet
          </h1>
          <p className="mt-2.5 text-sm leading-6 text-muted">
            Every run you start appears here — the question, what it established,
            and every source behind it, kept for as long as you want it.
          </p>
          <Link
            to="/"
            className="group mt-7 inline-flex items-center gap-2 rounded-xl bg-brand px-5 py-2.5 text-sm font-medium text-canvas shadow-sm transition-all hover:brightness-110 hover:shadow-brand/20"
          >
            Ask your first question
            <span className="transition-transform group-hover:translate-x-0.5">→</span>
          </Link>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-5xl animate-fade-up px-6 py-10">
      {/* "Every question that has been asked" was true when there was one
          history. It is now this account's history and nobody else's, and a
          subtitle that still claims otherwise is the interface making a
          promise the API stopped keeping. */}
      <Panel title="Your research" subtitle="Every question you have asked">
        {history.isLoading ? <Spinner label="Loading…" /> : null}
        {history.error ? <Failure message="Could not load the history." /> : null}
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
