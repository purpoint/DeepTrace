/**
 * The small shared pieces.
 *
 * Colours come from theme variables, so a component says `bg-surface` and is
 * correct in both themes. Writing `bg-white dark:bg-slate-900` on every element
 * is the same design maintained twice, and the copy is what drifts.
 *
 * A claim's status has one colour wherever it appears. A verdict rendered green
 * on the report page and grey on the claims page reads as two different
 * verdicts, and telling supported from unsupported at a glance is the entire
 * point of the verification layer.
 */

import type { ReactNode } from "react";

import type { ClaimStatus } from "../api/types";

const STATUS_STYLE: Record<string, string> = {
  supported: "bg-verdict-supported/10 text-verdict-supported ring-verdict-supported/25",
  partially_supported: "bg-verdict-partial/10 text-verdict-partial ring-verdict-partial/25",
  unsupported: "bg-verdict-unsupported/10 text-verdict-unsupported ring-verdict-unsupported/25",
  conflicting: "bg-verdict-conflicting/10 text-verdict-conflicting ring-verdict-conflicting/25",
  proposed: "bg-raised text-muted ring-line",
};

const STATUS_LABEL: Record<string, string> = {
  supported: "supported",
  partially_supported: "partly supported",
  unsupported: "not supported",
  conflicting: "sources disagree",
  proposed: "not checked",
};

export function StatusBadge({ status }: { status: ClaimStatus | string }) {
  return (
    <span
      className={`inline-flex shrink-0 items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
        STATUS_STYLE[status] ?? STATUS_STYLE.proposed
      }`}
    >
      {STATUS_LABEL[status] ?? status}
    </span>
  );
}

/** How a passage matched its source.
 *
 *  Shown distinctly from a quotation because they are different kinds of
 *  support: a paraphrase was matched by token overlap, so the wording the claim
 *  relies on is not the wording that was checked. */
export function QuoteBadge({ status }: { status: string }) {
  const quoted = status === "verbatim" || status === "normalised";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        quoted
          ? "bg-verdict-supported/10 text-verdict-supported"
          : "bg-verdict-partial/10 text-verdict-partial"
      }`}
      title={
        quoted
          ? "Found word for word in the source page"
          : "Matched by overlap, not quoted exactly"
      }
    >
      {quoted ? "✓ verbatim" : "≈ paraphrased"}
    </span>
  );
}

export function Panel({
  title,
  subtitle,
  children,
  actions,
}: {
  title: string;
  subtitle?: ReactNode;
  children: ReactNode;
  actions?: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-xl border border-line bg-surface shadow-sm">
      <header className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-3.5">
        <div>
          <h2 className="text-sm font-semibold tracking-tight text-ink">{title}</h2>
          {subtitle ? <p className="mt-0.5 text-xs text-faint">{subtitle}</p> : null}
        </div>
        {actions}
      </header>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-10 text-center text-sm text-faint">{children}</p>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2.5 py-10 text-sm text-faint">
      <span className="h-3.5 w-3.5 animate-spin rounded-full border-2 border-line border-t-brand" />
      {label}
    </div>
  );
}

/** A failure the user can act on.
 *
 *  Retryable failures get a button; the rest get an explanation. Offering "try
 *  again" for a 404 teaches people the button does nothing. */
export function Failure({
  message,
  onRetry,
  reference,
}: {
  message: string;
  onRetry?: () => void;
  // `| undefined` rather than just optional: with exactOptionalPropertyTypes,
  // a prop that may be absent is not the same as one that may be passed as
  // undefined, and callers here do the second.
  reference?: string | undefined;
}) {
  return (
    <div className="rounded-xl border border-verdict-unsupported/30 bg-verdict-unsupported/5 px-4 py-3 text-sm text-verdict-unsupported">
      <p>{message}</p>
      {reference ? (
        <p className="mt-1 font-mono text-xs opacity-70">reference {reference}</p>
      ) : null}
      {onRetry ? (
        <button
          onClick={onRetry}
          className="mt-2 rounded-md bg-verdict-unsupported/10 px-2.5 py-1 text-xs font-medium hover:bg-verdict-unsupported/20"
        >
          Try again
        </button>
      ) : null}
    </div>
  );
}

export function relativeTime(iso: string): string {
  const seconds = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}
