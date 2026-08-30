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
      {/* Every block below is held to a reading measure. Unconstrained, this
          panel ran to a median of 99 characters a line and a maximum of 160 --
          the reasoning is `text-xs`, so the full width of the card buys it far
          more characters than the claim above it. "The claims view is dense"
          was recorded as a content problem; most of it was line length.

          In `ch` per element rather than one width on the row, because the
          blocks are set at different sizes and a single pixel width would give
          each of them a different number of characters. */}
      <div className="flex items-start justify-between gap-4">
        {/* Text, always. A claim is model output over pages we do not control. */}
        <p className="max-w-[58ch] text-sm leading-6 text-ink">{claim.text}</p>
        <StatusBadge status={claim.status} />
      </div>

      {claim.condition ? (
        <p className="mt-1.5 max-w-[64ch] text-xs text-muted">Holds when: {claim.condition}</p>
      ) : null}

      {claim.reasoning ? (
        <p className="mt-2 max-w-[64ch] text-xs leading-5 text-faint">{claim.reasoning}</p>
      ) : null}

      {claim.overgeneralization ? (
        <p className="mt-2.5 max-w-[64ch] rounded-lg bg-verdict-partial/10 px-2.5 py-1.5 text-xs text-verdict-partial">
          Reaches past its evidence: {claim.overgeneralization}
        </p>
      ) : null}

      {claim.suggested_revision ? (
        <p className="mt-2 max-w-[64ch] rounded-lg bg-raised px-2.5 py-1.5 text-xs text-muted">
          Within the evidence: {claim.suggested_revision}
        </p>
      ) : null}

      <div className="mt-2.5 flex flex-wrap items-center gap-3 text-[11px] text-faint">
        <span className="font-mono">{claim.kind}</span>
        <span>strength {claim.strength.toFixed(2)}</span>
        <span>{claim.confidence} confidence</span>
        {claim.contradicted_by.length ? (
          <span className="text-verdict-conflicting">
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
                className={`rounded-lg px-2.5 py-1 text-xs transition-colors ${
                  filter === option.key
                    ? "bg-brand/15 text-brand ring-1 ring-inset ring-brand/30"
                    : "bg-raised text-muted hover:text-ink"
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
        <ul className="divide-y divide-line">
          {shown.map((claim) => (
            <Claim key={claim.id} claim={claim} />
          ))}
        </ul>
      )}
    </Panel>
  );
}
