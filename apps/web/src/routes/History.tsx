/** Everything that has been researched, newest first. */

import { Link } from "react-router-dom";

import { useHistory } from "../api/hooks";
import { Empty, Failure, Panel, Spinner, relativeTime } from "../components/ui";

const STATUS_COLOUR: Record<string, string> = {
  completed: "text-green-700",
  failed: "text-red-700",
  partial: "text-amber-700",
};

export function History() {
  const history = useHistory(50);

  return (
    <div className="mx-auto max-w-4xl px-6 py-10">
      <Panel title="Research history" subtitle="Every question that has been asked">
        {history.isLoading ? <Spinner label="Loading…" /> : null}
        {history.error ? <Failure message="Could not load the history." /> : null}
        {history.data?.length === 0 ? <Empty>Nothing has been researched yet.</Empty> : null}

        <ul className="divide-y divide-slate-100">
          {(history.data ?? []).map((run) => (
            <li key={run.research_id}>
              <Link
                to={`/research/${run.research_id}`}
                className="flex items-start justify-between gap-4 py-3 hover:bg-slate-50"
              >
                <div className="min-w-0">
                  <p className="truncate text-sm text-slate-900">{run.question}</p>
                  <p className="mt-0.5 text-xs text-slate-500">
                    {run.depth} · {relativeTime(run.created_at)}
                    {run.error ? ` · ${run.error.slice(0, 60)}` : ""}
                  </p>
                </div>
                <span
                  className={`shrink-0 text-xs ${STATUS_COLOUR[run.status] ?? "text-slate-500"}`}
                >
                  {run.status}
                </span>
              </Link>
            </li>
          ))}
        </ul>
      </Panel>
    </div>
  );
}
