/**
 * The pages that were read, and how good each was judged to be.
 *
 * Sorted by quality because that ordering is a claim the system makes and
 * should be inspectable: a reader who disagrees that a vendor blog outranks a
 * forum thread can see the judgement rather than having it hidden inside a
 * weighting.
 */

import { useSources } from "../api/hooks";
import { Empty, Failure, Panel, Spinner, relativeTime } from "../components/ui";

const TYPE_LABEL: Record<string, string> = {
  official_docs: "official docs",
  academic_paper: "academic",
  standard: "standard",
  engineering_blog: "engineering blog",
  technical_publication: "publication",
  community: "community",
  unknown: "unclassified",
};

export function Sources({ researchId, ready }: { researchId: string; ready: boolean }) {
  const sources = useSources(researchId, ready);

  if (!ready) return <Panel title="Sources"><Empty>Nothing retrieved yet.</Empty></Panel>;
  if (sources.isLoading) return <Spinner label="Loading sources…" />;
  if (sources.error) return <Failure message="Could not load the sources." />;

  const all = sources.data ?? [];

  return (
    <Panel title="Sources" subtitle={`${all.length} pages retrieved and scored`}>
      {all.length === 0 ? (
        <Empty>No sources were retrieved.</Empty>
      ) : (
        <ul className="divide-y divide-line">
          {all.map((source) => (
            <li key={source.id} className="flex items-start justify-between gap-4 py-3">
              <div className="min-w-0">
                <a
                  href={source.url}
                  target="_blank"
                  rel="noopener noreferrer nofollow ugc"
                  className="block truncate text-sm font-medium text-ink transition-colors hover:text-brand"
                >
                  {/* A page title, written by whoever owns the page. Text only. */}
                  {source.title || source.url}
                </a>
                <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-faint">
                  <span>{source.domain}</span>
                  <span className="rounded bg-raised px-1.5 py-0.5 font-mono">
                    {TYPE_LABEL[source.source_type] ?? source.source_type}
                  </span>
                  <span>{source.word_count.toLocaleString()} words</span>
                  <span>{relativeTime(source.retrieved_at)}</span>
                  {source.fetch_failed ? (
                    <span className="text-verdict-partial">could not be fetched</span>
                  ) : null}
                </div>
              </div>
              <div className="shrink-0 text-right">
                <div className="text-sm font-medium tabular-nums text-ink">
                  {source.quality_score.toFixed(2)}
                </div>
                <div className="text-[11px] text-faint">quality</div>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Panel>
  );
}
