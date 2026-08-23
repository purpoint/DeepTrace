/**
 * The report, with its citations resolvable in place.
 *
 * The document is markdown containing `[3]` markers, and the citation table is
 * structured. Rendering the markdown alone would leave a reader clicking away
 * to check anything; showing the citations beside it means the passage behind a
 * number is one glance away, which is the difference between a citation that is
 * checkable and one that is merely present.
 */

import { useReport } from "../api/hooks";
import type { Citation } from "../api/types";
import { Markdown } from "../components/Markdown";
import { Failure, Panel, QuoteBadge, Spinner } from "../components/ui";

function CitationCard({ citation }: { citation: Citation }) {
  return (
    <li id={`citation-${citation.number}`} className="scroll-mt-4 py-3">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xs text-slate-400">[{citation.number}]</span>
        <a
          href={citation.url}
          target="_blank"
          rel="noopener noreferrer nofollow ugc"
          className="text-sm font-medium text-blue-700 underline-offset-2 hover:underline"
        >
          {/* Text, not markup: a page title is written by whoever owns the page. */}
          {citation.title || citation.domain}
        </a>
      </div>
      <blockquote className="mt-1 border-l-2 border-slate-200 pl-3 text-sm text-slate-600">
        “{citation.quote}”
      </blockquote>
      <div className="mt-1 flex items-center gap-2 text-xs text-slate-400">
        <QuoteBadge status={citation.quote_status} />
        <span>{citation.domain}</span>
        {citation.location ? <span>· {citation.location}</span> : null}
      </div>
    </li>
  );
}

export function Report({ researchId, ready }: { researchId: string; ready: boolean }) {
  const report = useReport(researchId, ready);

  if (!ready) {
    return (
      <Panel title="Report">
        <p className="py-6 text-sm text-slate-500">
          The report is written once every claim has been checked.
        </p>
      </Panel>
    );
  }
  if (report.isLoading) return <Spinner label="Loading the report…" />;
  if (report.error) {
    return (
      <Failure
        message="This research did not produce a report. Its evidence and claims are still available."
      />
    );
  }
  if (!report.data) return null;

  return (
    <div className="space-y-4">
      {!report.data.fully_cited ? (
        // Surfaced rather than hidden: assembly removed citations that pointed
        // at nothing, and a reader deserves to know the document was edited.
        <p className="rounded-md bg-amber-50 px-3 py-2 text-sm text-amber-800">
          Some citations in the draft pointed at passages that do not exist and were
          removed before this was shown.
        </p>
      ) : null}

      <article className="rounded-lg border border-slate-200 bg-white px-8 py-6">
        <Markdown>{report.data.markdown}</Markdown>
      </article>

      <Panel
        title="Citations"
        subtitle={`${report.data.citations} passages, each checked against its page`}
      >
        <ul className="divide-y divide-slate-100">
          {report.data.structured.citations.map((citation) => (
            <CitationCard key={citation.number} citation={citation} />
          ))}
        </ul>
      </Panel>
    </div>
  );
}
