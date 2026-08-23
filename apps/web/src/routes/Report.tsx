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
import { Reveal } from "../components/Reveal";
import { Failure, Panel, QuoteBadge, Spinner } from "../components/ui";

function CitationCard({ citation }: { citation: Citation }) {
  return (
    <li id={`citation-${citation.number}`} className="citation scroll-mt-24 rounded-lg px-2 py-3">
      <div className="flex items-baseline gap-2">
        <span className="font-mono text-xs text-brand">[{citation.number}]</span>
        <a
          href={citation.url}
          target="_blank"
          rel="noopener noreferrer nofollow ugc"
          className="text-sm font-medium text-ink underline-offset-2 hover:text-brand hover:underline"
        >
          {/* Text, not markup: a page title is written by whoever owns the page. */}
          {citation.title || citation.domain}
        </a>
      </div>
      <blockquote className="mt-1.5 border-l-2 border-brand/30 pl-3 text-sm leading-6 text-muted">
        “{citation.quote}”
      </blockquote>
      <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-faint">
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
        <p className="py-8 text-sm text-faint">
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
        <p className="rounded-xl bg-verdict-partial/10 px-3.5 py-2.5 text-sm text-verdict-partial ring-1 ring-inset ring-verdict-partial/25">
          Some citations in the draft pointed at passages that do not exist and were
          removed before this was shown.
        </p>
      ) : null}

      <Reveal>
        <article className="rounded-xl border border-line bg-surface px-8 py-7 shadow-sm">
          {/* Citation markers become anchors here, so `[3]` in the prose jumps
              to the passage behind it. A citation a reader cannot follow is a
              citation that only looks like provenance. */}
          <Markdown linkCitationMarkers>{report.data.markdown}</Markdown>
        </article>
      </Reveal>

      <Panel
        title="Citations"
        subtitle={`${report.data.citations} passages, each checked against its page`}
      >
        {/* Deliberately not revealed on scroll. A citation is a jump target,
            and a target that is invisible until an observer notices it means
            clicking [3] lands the reader on nothing. Found by clicking one:
            the element was in the right place with opacity zero. */}
        <ul className="divide-y divide-line">
          {report.data.structured.citations.map((citation) => (
            <CitationCard key={citation.number} citation={citation} />
          ))}
        </ul>
      </Panel>
    </div>
  );
}
