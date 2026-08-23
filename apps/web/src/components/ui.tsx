/**
 * The small shared pieces.
 *
 * A claim's status has one colour wherever it appears. A verdict rendered green
 * on the report page and grey on the claims page reads to a user as two
 * different verdicts, and the whole point of the verification layer is that a
 * reader can tell supported from unsupported at a glance.
 */

import type { ReactNode } from "react";

import type { ClaimStatus } from "../api/types";

const STATUS_STYLE: Record<string, string> = {
  supported: "bg-green-50 text-green-800 ring-green-600/20",
  partially_supported: "bg-amber-50 text-amber-800 ring-amber-600/20",
  unsupported: "bg-red-50 text-red-800 ring-red-600/20",
  conflicting: "bg-violet-50 text-violet-800 ring-violet-600/20",
  proposed: "bg-slate-100 text-slate-700 ring-slate-500/20",
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
      className={`inline-flex items-center rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${
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
 *  relies on is not the wording that was checked. Rendering them identically
 *  would present the weaker one as the stronger. */
export function QuoteBadge({ status }: { status: string }) {
  const quoted = status === "verbatim" || status === "normalised";
  return (
    <span
      className={`inline-flex items-center gap-1 rounded px-1.5 py-0.5 font-mono text-[11px] ${
        quoted ? "bg-slate-100 text-slate-600" : "bg-amber-50 text-amber-700"
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
    <section className="rounded-lg border border-slate-200 bg-white">
      <header className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-3">
        <div>
          <h2 className="text-sm font-semibold text-slate-900">{title}</h2>
          {subtitle ? <p className="mt-0.5 text-xs text-slate-500">{subtitle}</p> : null}
        </div>
        {actions}
      </header>
      <div className="px-5 py-4">{children}</div>
    </section>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-8 text-center text-sm text-slate-500">{children}</p>;
}

export function Spinner({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-slate-500">
      <span className="h-3 w-3 animate-spin rounded-full border-2 border-slate-300 border-t-slate-600" />
      {label}
    </div>
  );
}

/** A failure the user can act on.
 *
 *  Retryable failures get a button; the rest get an explanation. Offering
 *  "try again" for a 404 teaches people that the button does nothing. */
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
    <div className="rounded-md border border-red-200 bg-red-50 px-4 py-3 text-sm text-red-800">
      <p>{message}</p>
      {reference ? (
        <p className="mt-1 font-mono text-xs text-red-600">reference {reference}</p>
      ) : null}
      {onRetry ? (
        <button
          onClick={onRetry}
          className="mt-2 rounded bg-red-100 px-2 py-1 text-xs font-medium hover:bg-red-200"
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
