/**
 * The short version of a run, as a card.
 *
 * A research report is nine sections and several thousand words, and most of
 * the time what someone wants back is the one sentence plus enough context to
 * know whether to trust it. That is what this is.
 *
 * **It never becomes an uncited answer.** A card showing a confident sentence
 * and nothing else is precisely the failure this whole system exists to
 * prevent -- it would be a chatbot reply wearing the product's clothes. So the
 * card carries its own support: how many claims survived verification, how many
 * sources were cited, and whether every marker in the report resolved. If the
 * answer is weakly supported, the card says so on its face rather than in a tab
 * the reader has to go and find.
 *
 * **Nothing here is generated.** The question comes from the run, the answer is
 * the report's own summary section -- itself written only from claims that
 * survived verification -- and the counts are read off the record. No model
 * call, no cost, and no opportunity for the card to say something the report
 * does not.
 *
 * The visual debt to a paper flash card is deliberate but partial: the numbered
 * corner, the Q and A badges, the rule under the question, the tag chip. The
 * paper itself is not, because this application is dark, quiet and technical,
 * and a notepad texture with doodles would read as a different product.
 */

import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import type { ReportView, ResearchDetail } from "../api/types";

/** Strip the trailing citation run from the summary sentence.
 *
 *  A summary commonly ends "[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]", which is honest
 *  in a report and unreadable on a card. Only a *trailing* cluster is removed,
 *  and only because the card states the citation count on its face -- the
 *  support is still declared, in a form that fits. Markers in the middle of a
 *  sentence are left exactly where they are. */
export function trimTrailingCitations(text: string): string {
  return text.replace(/\s*\[[\d,\s]+\]\s*\.?\s*$/, ".").trim();
}

function Badge({ letter, label, tone }: { letter: string; label: string; tone: "brand" | "ok" }) {
  const colour =
    tone === "brand"
      ? "bg-brand/15 text-brand ring-brand/30"
      : "bg-verdict-supported/15 text-verdict-supported ring-verdict-supported/30";
  return (
    <div className="flex items-center gap-2">
      <span
        className={`flex h-5 w-5 items-center justify-center rounded-md text-[11px] font-semibold ring-1 ring-inset ${colour}`}
      >
        {letter}
      </span>
      <span className="text-[11px] font-semibold uppercase tracking-wider text-muted">
        {label}
      </span>
    </div>
  );
}

function Chip({ children, tone = "muted" }: { children: React.ReactNode; tone?: "muted" | "warn" }) {
  const colour =
    tone === "warn"
      ? "bg-verdict-partial/10 text-verdict-partial ring-verdict-partial/25"
      : "bg-raised text-muted ring-line";
  return (
    <span className={`rounded-md px-2 py-1 text-xs ring-1 ring-inset ${colour}`}>{children}</span>
  );
}

export function SummaryCard({
  detail,
  report,
  onClose,
}: {
  detail: ResearchDetail;
  report: ReportView;
  onClose: () => void;
}) {
  const panel = useRef<HTMLDivElement>(null);

  // Escape closes, and focus moves into the dialog so a keyboard user is not
  // left tabbing through the page behind it.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    document.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [onClose]);

  const summary = report.structured.sections?.find((section) => section.kind === "summary");
  const answer = summary ? trimTrailingCitations(summary.body) : null;

  // Rendered into document.body rather than in place.
  //
  // `position: fixed` is only fixed to the viewport if no ancestor has a
  // transform -- a transform makes the element a containing block, and the
  // modal then positions against *it*. The tab content here sits inside an
  // `animate-slide-in` wrapper, so the backdrop resolved against a 4502px-tall
  // element and centred this card at 2263px: present in the DOM, fully opaque,
  // correctly sized, and entirely below the fold.
  //
  // A portal is the fix that cannot be re-broken by someone adding an
  // animation to a parent later, which is why it is preferred over hunting
  // down the transform.
  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4 backdrop-blur-sm"
      onClick={onClose}
      role="presentation"
    >
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Summary card"
        tabIndex={-1}
        // Stops a click inside the card from reaching the backdrop and closing
        // it, which is the single most irritating modal bug there is.
        onClick={(event) => event.stopPropagation()}
        className="w-full max-w-xl overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl outline-none animate-fade-up"
      >
        <div className="flex items-start justify-between border-b border-line px-6 py-4">
          <div>
            <h2 className="text-sm font-semibold tracking-tight text-ink">Summary card</h2>
            <div className="mt-1 h-0.5 w-10 rounded-full bg-brand/60" />
          </div>
          {/* The run id rather than a card number. A "#06" means nothing an
              hour later; this is the thing you paste to find the run again. */}
          <span className="font-mono text-[11px] text-faint">{detail.research_id}</span>
        </div>

        <div className="space-y-5 px-6 py-5">
          <section>
            <Badge letter="Q" label="Question" tone="brand" />
            <p className="mt-2 text-[15px] leading-relaxed text-ink">{detail.question}</p>
            <div className="mt-2 h-px w-full bg-gradient-to-r from-brand/40 to-transparent" />
          </section>

          <section>
            <Badge letter="A" label="Answer" tone="ok" />
            {answer ? (
              <p className="mt-2 text-[15px] leading-relaxed text-muted">{answer}</p>
            ) : (
              // Said plainly rather than shown blank. A card with an empty
              // answer looks broken; a card that says the run produced no
              // summary is reporting something true.
              <p className="mt-2 text-[15px] leading-relaxed text-faint">
                This run produced no summary section.
              </p>
            )}
          </section>

          <section className="flex flex-wrap gap-2 border-t border-line pt-4">
            {detail.research_type ? <Chip>{detail.research_type}</Chip> : null}
            <Chip>
              {detail.claims} {detail.claims === 1 ? "claim" : "claims"} verified
            </Chip>
            <Chip>
              {report.citations} {report.citations === 1 ? "citation" : "citations"}
            </Chip>
            <Chip>
              {detail.sources} {detail.sources === 1 ? "source" : "sources"}
            </Chip>
            {/* The one chip that can be bad news, and it is not hidden. */}
            {report.fully_cited ? null : <Chip tone="warn">some citations unresolved</Chip>}
          </section>
        </div>

        <div className="flex items-center justify-between border-t border-line bg-raised/40 px-6 py-3">
          <span className="text-xs text-faint">
            {report.fully_cited
              ? "Every citation resolves to a source in the report."
              : "Open the report to see which citations did not resolve."}
          </span>
          <button
            onClick={onClose}
            className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-raised hover:text-ink"
          >
            Close
          </button>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The button that opens it, and the state it owns. */
export function SummaryCardButton({
  detail,
  report,
}: {
  detail: ResearchDetail;
  report: ReportView;
}) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={() => setOpen(true)}
        className="inline-flex items-center gap-1.5 rounded-lg border border-line bg-surface px-3 py-1.5 text-xs font-medium text-muted transition-colors hover:border-brand/40 hover:text-ink"
      >
        <span className="text-brand">⌗</span> Summary card
      </button>
      {open ? (
        <SummaryCard detail={detail} report={report} onClose={() => setOpen(false)} />
      ) : null}
    </>
  );
}
