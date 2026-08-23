/**
 * One run, from every angle.
 *
 * The tabs are the product's argument made navigable: a report you can read, and
 * beneath it the claims it rests on, the passages behind those, the pages behind
 * those, and the record of every call that produced them. A reader who doubts a
 * sentence can descend, one tab at a time, to the page it came from.
 *
 * Which tab opens first depends on the run. A finished run opens on its report,
 * because that is what was asked for; a running one opens on progress, because
 * there is nothing else yet and a user watching an empty report page assumes
 * something is broken.
 */

import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";

import { useResearch } from "../api/hooks";
import { ApiError } from "../api/client";
import { Failure, Spinner, relativeTime } from "../components/ui";
import { Claims } from "./Claims";
import { Evidence } from "./Evidence";
import { ProgressView } from "./Progress";
import { Report } from "./Report";
import { Sources } from "./Sources";
import { Trace } from "./Trace";

type Tab = "progress" | "report" | "claims" | "evidence" | "sources" | "trace";

const FINISHED = new Set(["completed", "failed", "cancelled", "partial"]);

export function Workspace() {
  const { researchId = "" } = useParams();
  const [tab, setTab] = useState<Tab | null>(null);

  // Live progress is what a running run needs; polling is the fallback the
  // detail query uses when no socket is available.
  const detail = useResearch(researchId, { live: true });
  const status = detail.data?.status ?? "queued";
  const finished = FINISHED.has(status);
  const ready = Boolean(detail.data && detail.data.claims > 0);

  useEffect(() => {
    // Chosen once, when the run's state is first known. Re-deciding on every
    // render would drag a reader back to the report the moment a run finished
    // while they were reading its trace.
    if (tab === null && detail.data) setTab(finished ? "report" : "progress");
  }, [detail.data, finished, tab]);

  if (detail.isLoading) return <Spinner label="Loading…" />;
  if (detail.error) {
    const error = detail.error as ApiError;
    return (
      <div className="mx-auto max-w-2xl px-6 py-16">
        <Failure
          message={
            error.code === "not_found"
              ? "There is no research with that id. It may have expired, or the link may be wrong."
              : error.message
          }
          reference={error.reference}
        />
        <Link to="/" className="mt-4 inline-block text-sm text-blue-700 hover:underline">
          Ask something else
        </Link>
      </div>
    );
  }
  if (!detail.data) return null;

  const tabs: { key: Tab; label: string; count?: number }[] = [
    { key: "progress", label: "Progress" },
    { key: "report", label: "Report" },
    { key: "claims", label: "Claims", count: detail.data.claims },
    { key: "evidence", label: "Evidence", count: detail.data.evidence },
    { key: "sources", label: "Sources", count: detail.data.sources },
    { key: "trace", label: "Trace" },
  ];

  return (
    <div className="mx-auto max-w-4xl px-6 py-8">
      <header className="mb-6">
        {/* The user's own question. Rendered as text regardless. */}
        <h1 className="text-xl font-semibold text-slate-900">{detail.data.question}</h1>
        <p className="mt-1 text-xs text-slate-500">
          {detail.data.depth} · started {relativeTime(detail.data.created_at)} ·{" "}
          <span className={finished ? "" : "text-slate-900"}>{status}</span>
          {detail.data.job && detail.data.job.attempts > 1
            ? ` · attempt ${detail.data.job.attempts}`
            : ""}
        </p>
        {detail.data.error ? (
          <p className="mt-2 rounded bg-red-50 px-3 py-2 text-sm text-red-800">
            {detail.data.error}
          </p>
        ) : null}
      </header>

      <nav className="mb-4 flex gap-1 border-b border-slate-200">
        {tabs.map((entry) => (
          <button
            key={entry.key}
            onClick={() => setTab(entry.key)}
            className={`-mb-px border-b-2 px-3 py-2 text-sm ${
              tab === entry.key
                ? "border-slate-900 font-medium text-slate-900"
                : "border-transparent text-slate-500 hover:text-slate-800"
            }`}
          >
            {entry.label}
            {entry.count ? (
              <span className="ml-1.5 text-xs text-slate-400">{entry.count}</span>
            ) : null}
          </button>
        ))}
      </nav>

      {tab === "progress" ? <ProgressView detail={detail.data} /> : null}
      {tab === "report" ? <Report researchId={researchId} ready={detail.data.has_report} /> : null}
      {tab === "claims" ? <Claims researchId={researchId} ready={ready} /> : null}
      {tab === "evidence" ? <Evidence researchId={researchId} ready={ready} /> : null}
      {tab === "sources" ? (
        <Sources researchId={researchId} ready={detail.data.sources > 0} />
      ) : null}
      {tab === "trace" ? <Trace researchId={researchId} ready={finished} /> : null}
    </div>
  );
}
