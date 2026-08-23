/**
 * What the research asserts, and what checking each one concluded.
 *
 * Unsupported claims are shown, not hidden. They are the evidence that
 * verification did something: a page listing only what survived looks identical
 * whether the checker rejected three claims or was never run.
 */

import { useState } from "react";

import { useClaims } from "../api/hooks";
import type { ClaimView } from "../api/types";
import { Empty, Failure, Panel, Spinner, StatusBadge } from "../components/ui";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "supported", label: "Supported" },
  { key: "partially_supported", label: "Partly supported" },
  { key: "conflicting", label: "Disputed" },
  { key: "unsupported", label: "Not supported" },
] as const;

function Claim({ claim }: { claim: ClaimView }) {
  return (
    <li className="py-4">
      <div className="flex items-start justify-between gap-4">
        {/* Text, always. A claim is model output over pages we do not control. */}
        <p className="text-sm text-slate-900">{claim.text}</p>
        <StatusBadge status={claim.status} />
      </div>

      {claim.condition ? (
        <p className="mt-1 text-xs text-slate-600">Holds when: {claim.condition}</p>
      ) : null}

      {claim.reasoning ? (
        <p className="mt-2 text-xs leading-5 text-slate-500">{claim.reasoning}</p>
      ) : null}

      {claim.overgeneralization ? (
        <p className="mt-2 rounded bg-amber-50 px-2 py-1 text-xs text-amber-800">
          Reaches past its evidence: {claim.overgeneralization}
        </p>
      ) : null}

      {claim.suggested_revision ? (
        <p className="mt-2 rounded bg-slate-50 px-2 py-1 text-xs text-slate-700">
          Within the evidence: {claim.suggested_revision}
        </p>
      ) : null}

      <div className="mt-2 flex flex-wrap items-center gap-3 text-[11px] text-slate-400">
        <span className="font-mono">{claim.kind}</span>
        <span>strength {claim.strength.toFixed(2)}</span>
        <span>{claim.confidence} confidence</span>
        {claim.contradicted_by.length ? (
          <span className="text-violet-700">
            contradicted by {claim.contradicted_by.length} passage
            {claim.contradicted_by.length > 1 ? "s" : ""}
          </span>
        ) : null}
      </div>
    </li>
  );
}

export function Claims({ researchId, ready }: { researchId: string; ready: boolean }) {
  const [filter, setFilter] = useState<string>("all");
  const claims = useClaims(researchId, ready);

  if (!ready) return <Panel title="Claims"><Empty>Not yet derived.</Empty></Panel>;
  if (claims.isLoading) return <Spinner label="Loading claims…" />;
  if (claims.error) return <Failure message="Could not load the claims." />;

  const all = claims.data ?? [];
  const shown = filter === "all" ? all : all.filter((claim) => claim.status === filter);
  const counts = all.reduce<Record<string, number>>((tally, claim) => {
    tally[claim.status] = (tally[claim.status] ?? 0) + 1;
    return tally;
  }, {});

  return (
    <Panel
      title="Claims"
      subtitle={`${all.length} assertions, each checked against the evidence`}
      actions={
        <div className="flex flex-wrap gap-1">
          {FILTERS.map((option) => {
            const count = option.key === "all" ? all.length : (counts[option.key] ?? 0);
            if (option.key !== "all" && count === 0) return null;
            return (
              <button
                key={option.key}
                onClick={() => setFilter(option.key)}
                className={`rounded px-2 py-0.5 text-xs ${
                  filter === option.key
                    ? "bg-slate-900 text-white"
                    : "bg-slate-100 text-slate-600 hover:bg-slate-200"
                }`}
              >
                {option.label} {count}
              </button>
            );
          })}
        </div>
      }
    >
      {shown.length === 0 ? (
        <Empty>No claims with this verdict.</Empty>
      ) : (
        <ul className="divide-y divide-slate-100">
          {shown.map((claim) => (
            <Claim key={claim.id} claim={claim} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
