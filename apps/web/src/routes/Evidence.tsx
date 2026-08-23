/**
 * The passages, and whether each was actually found in its page.
 *
 * The quotation status is the most important thing on this screen. A verbatim
 * match and a paraphrase are different kinds of support -- one was checked
 * against the exact wording, the other by overlap -- and showing them
 * identically would present the weaker as the stronger.
 */

import { useState } from "react";

import { useEvidence, useSources } from "../api/hooks";
import { Empty, Failure, Panel, QuoteBadge, Spinner } from "../components/ui";

export function Evidence({ researchId, ready }: { researchId: string; ready: boolean }) {
  const [quotedOnly, setQuotedOnly] = useState(false);
  const evidence = useEvidence(researchId, ready);
  const sources = useSources(researchId, ready);

  if (!ready) return <Panel title="Evidence"><Empty>Not yet extracted.</Empty></Panel>;
  if (evidence.isLoading) return <Spinner label="Loading evidence…" />;
  if (evidence.error) return <Failure message="Could not load the evidence." />;

  const byId = new Map((sources.data ?? []).map((source) => [source.id, source]));
  const all = evidence.data ?? [];
  const shown = quotedOnly
    ? all.filter((item) => item.quote_status === "verbatim" || item.quote_status === "normalised")
    : all;
  const verbatim = all.filter(
    (item) => item.quote_status === "verbatim" || item.quote_status === "normalised",
  ).length;

  return (
    <Panel
      title="Evidence"
      subtitle={`${all.length} passages, ${verbatim} found word for word in their page`}
      actions={
        <label className="flex items-center gap-2 text-xs text-slate-600">
          <input
            type="checkbox"
            checked={quotedOnly}
            onChange={(event) => setQuotedOnly(event.target.checked)}
          />
          Quoted only
        </label>
      }
    >
      {shown.length === 0 ? (
        <Empty>No passages match.</Empty>
      ) : (
        <ul className="divide-y divide-slate-100">
          {shown.map((item) => {
            const source = byId.get(item.source_id);
            return (
              <li key={item.id} className="py-4">
                <p className="text-sm font-medium text-slate-900">{item.claim}</p>
                <blockquote className="mt-1 border-l-2 border-slate-200 pl-3 text-sm text-slate-600">
                  “{item.supporting_text}”
                </blockquote>
                <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-400">
                  <QuoteBadge status={item.quote_status} />
                  {source ? (
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noopener noreferrer nofollow ugc"
                      className="text-blue-700 hover:underline"
                    >
                      {source.domain}
                    </a>
                  ) : (
                    <span>source unavailable</span>
                  )}
                  <span>weight {item.weight.toFixed(2)}</span>
                  {item.task_id ? <span className="font-mono">{item.task_id}</span> : null}
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Panel>
  );
}
