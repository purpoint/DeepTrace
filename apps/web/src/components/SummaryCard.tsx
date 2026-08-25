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

import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { useReport, useResearch } from "../api/hooks";
import type { ReportView, ResearchDetail } from "../api/types";

export type SwipeOutcome = "dismiss" | "spring-back" | "ignore";

/** What a completed swipe should do.
 *
 *  Pulled out of the component and made pure, because jsdom's PointerEvent
 *  carries neither `clientY` nor `pointerType` -- so a test that fires pointer
 *  events at the card exercises nothing, and the three that asserted "the card
 *  did not close" passed for the reason that nothing had happened at all.
 *
 *  This is the part with the decisions in it. The React wiring around it is
 *  three handlers and a transform, and is verified in a real browser. */
export function swipeOutcome(options: {
  pointerType: string;
  startY: number;
  endY: number;
  contentScrolled: boolean;
}): SwipeOutcome {
  // Touch only. A mouse drag would fight text selection, and selecting the
  // answer to copy is a thing people do on a card built to be taken elsewhere.
  if (options.pointerType !== "touch") return "ignore";
  // A swipe that began while the answer was scrolled is a scroll, not a
  // dismissal. Taking the card away there is the standard way this gesture is
  // got wrong.
  if (options.contentScrolled) return "ignore";
  return options.endY - options.startY > DISMISS_PX ? "dismiss" : "spring-back";
}

const DISMISS_PX = 110;
/* How far the card must travel before a swipe counts as a dismissal. Far
   enough that a scroll gesture caught at the top of the content does not throw
   it away, short enough that dismissing does not feel like work. */

const EXIT_MS = 160;
/*How long the card takes to leave. Must match the `card-out` animation in the
Tailwind theme: shorter here and the card is torn off mid-flight, longer and it
sits invisible for a beat while the page waits on a timer.*/
import { Spinner } from "./ui";

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

/** The card as plain text, for pasting into notes or a chat.
 *
 *  Deliberately carries the support and the run id with it. A question and an
 *  answer pasted into Slack with nothing else is the uncited answer this card
 *  is built to avoid, only now it has escaped the product entirely and nobody
 *  can tell where it came from. The id is what makes it traceable back. */
export function asPlainText(
  detail: ResearchDetail,
  report: ReportView,
  answer: string | null,
): string {
  const support = [
    detail.research_type,
    `${detail.claims} claims verified`,
    `${report.citations} citations`,
    `${detail.sources} sources`,
    report.fully_cited ? null : "some citations unresolved",
  ]
    .filter(Boolean)
    .join(" · ");

  return [
    `Q: ${detail.question}`,
    "",
    `A: ${answer ?? "This run produced no summary section."}`,
    "",
    `— DeepTrace · ${support}`,
    detail.research_id,
  ].join("\n");
}

function CopyButton({ text }: { text: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");

  const copy = async () => {
    try {
      // Unavailable on an insecure origin and in older browsers, so the
      // failure is shown rather than swallowed -- a button that silently does
      // nothing is worse than one that says it could not.
      await navigator.clipboard.writeText(text);
      setState("copied");
    } catch {
      setState("failed");
    }
    window.setTimeout(() => setState("idle"), 2000);
  };

  return (
    <button
      onClick={copy}
      className="rounded-lg border border-line px-2.5 py-1 text-xs text-muted transition-colors hover:border-brand/40 hover:text-ink"
    >
      {state === "copied" ? "Copied" : state === "failed" ? "Could not copy" : "Copy"}
    </button>
  );
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
  const [leaving, setLeaving] = useState(false);
  const leavingRef = useRef(false);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Dismissal plays the exit animation before unmounting.
  //
  // Without this the card vanishes on the frame the state changes, and an
  // entrance that was animated followed by a disappearance that was not reads
  // worse than no animation at all -- the eye notices the asymmetry even when
  // the viewer could not name it. The timeout matches `card-out`, and it is
  // the one number here that has to stay in step with the stylesheet.
  // The guard reads a ref, not the state.
  //
  // `leaving` in a dependency array is captured by the keydown listener when
  // it is registered, so a second Escape ran against a closure where it was
  // still false and queued a second unmount -- four presses, four calls to
  // onClose, the last three arriving after the card was already gone. The
  // guard looked right and did nothing, which is the usual shape of this.
  const dismiss = useCallback(() => {
    if (leavingRef.current) return;
    leavingRef.current = true;
    setLeaving(true);
    window.setTimeout(onClose, EXIT_MS);
  }, [onClose]);

  // Swipe down to dismiss.
  //
  // Touch only. A mouse drag would fight text selection in the answer, and
  // selecting a sentence to copy is a thing people do on a card whose whole
  // purpose is being taken elsewhere.
  const drag = useRef<{ startY: number; scrolled: boolean } | null>(null);
  const [offset, setOffset] = useState(0);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.pointerType !== "touch" || leavingRef.current) return;
    // Only from the top of the content. Otherwise a swipe meant to scroll a
    // long answer drags the whole card away instead, which is the standard way
    // this gesture is got wrong.
    const body = bodyRef.current;
    drag.current = { startY: event.clientY, scrolled: Boolean(body && body.scrollTop > 0) };
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!drag.current || drag.current.scrolled) return;
    const delta = event.clientY - drag.current.startY;
    // Downward follows the finger; upward is heavily resisted, so the card can
    // be nudged but never flung off the top of the screen.
    setOffset(delta > 0 ? delta : delta / 6);
  };

  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const started = drag.current;
    drag.current = null;
    if (!started) return;

    // Measured from the pointer, not from React state. Reading `offset` would
    // depend on the move's re-render having committed first, which is the same
    // stale-state trap the dismissal guard already fell into.
    const outcome = swipeOutcome({
      pointerType: event.pointerType,
      startY: started.startY,
      endY: event.clientY,
      contentScrolled: started.scrolled,
    });

    if (outcome === "dismiss") {
      dismiss();
      return;
    }
    setOffset(0);
  };

  // Escape closes, and focus moves into the dialog so a keyboard user is not
  // left tabbing through the page behind it.
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") dismiss();
    };
    document.addEventListener("keydown", onKey);
    panel.current?.focus();
    return () => document.removeEventListener("keydown", onKey);
  }, [dismiss]);

  // The page behind must not scroll while the card is open. A modal that lets
  // the document move underneath it feels like a sticker rather than a layer.
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

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
      className={`fixed inset-0 z-50 flex items-center justify-center p-4 ${
        leaving ? "animate-backdrop-out" : "animate-backdrop-in"
      }`}
      onClick={dismiss}
      role="presentation"
    >
      {/* Two layers rather than one. The wash dims the page and the blur pushes
          it out of focus; separating them means the blur can be strong without
          the whole screen going black.

          The wash is dark in BOTH themes, which is the part that was wrong.
          Tinting it with the canvas colour meant light mode painted white over
          white: the page behind read as bleached rather than dimmed, and a
          white card on a white-washed page has almost no edge to find. A scrim
          exists to push the page back, and pushing back means darker. */}
      <div className="absolute inset-0 bg-slate-950/40 backdrop-blur-md dark:bg-canvas/85" />

      {/* The bloom. A brand-coloured light behind the card, which is what makes
          it read as lifted off the page rather than pasted onto it -- a flat
          card on a flat backdrop has no depth for the eye to find. Kept faint:
          this is lighting, not an effect. */}
      {/* Stronger in light. The light palette's brand is a deep teal rather
          than the dark palette's bright cyan, and at nine percent it simply
          did not exist -- the effect was tuned against one theme and checked
          in one theme. */}
      <div
        aria-hidden
        className="pointer-events-none absolute h-[36rem] w-[36rem] animate-bloom rounded-full bg-brand/20 blur-[110px] dark:bg-brand/[0.09]"
      />
      <div
        ref={panel}
        role="dialog"
        aria-modal="true"
        aria-label="Summary card"
        tabIndex={-1}
        // Stops a click inside the card from reaching the backdrop and closing
        // it, which is the single most irritating modal bug there is.
        onClick={(event) => event.stopPropagation()}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={endDrag}
        onPointerCancel={endDrag}
        style={
          offset
            ? {
                // While a finger is on it the animation must not fight the
                // transform, so the entrance is dropped for the duration.
                transform: `translateY(${offset}px)`,
                animation: "none",
                // Fades as it goes, which is what tells the user the gesture is
                // doing something before they have committed to it.
                opacity: Math.max(0.4, 1 - offset / 400),
              }
            : undefined
        }
        className={`relative w-full max-w-xl overflow-hidden rounded-2xl border border-line bg-surface shadow-2xl shadow-slate-900/20 outline-none ring-1 ring-brand/10 transition-transform dark:shadow-black/40 ${
          drag.current ? "duration-0" : "duration-200"
        } ${leaving ? "animate-card-out" : "animate-card-in"}`}
      >
        {/* The grab handle, on touch only. A swipe gesture nobody can see is a
            gesture nobody uses. */}
        <div
          aria-hidden
          className="mx-auto mt-2 h-1 w-10 rounded-full bg-line sm:hidden"
        />
        {/* A hairline of brand colour along the top edge, brightest in the
            middle. Cheap, and it is what stops the card reading as a plain
            rectangle without adding anything a reader has to look at. */}
        <div
          aria-hidden
          className="absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-brand/50 to-transparent"
        />
        <div
          className="flex items-start justify-between border-b border-line px-6 py-4 animate-slide-in"
          style={{ animationDelay: "60ms" }}
        >
          <div>
            <h2 className="text-sm font-semibold tracking-tight text-ink">Summary card</h2>
            <div className="mt-1 h-0.5 w-10 rounded-full bg-brand/60" />
          </div>
          {/* The run id rather than a card number. A "#06" means nothing an
              hour later; this is the thing you paste to find the run again. */}
          <span className="font-mono text-[11px] text-faint">{detail.research_id}</span>
        </div>

        <div ref={bodyRef} className="max-h-[60vh] space-y-5 overflow-y-auto px-6 py-5">
          <section className="animate-slide-in" style={{ animationDelay: "120ms" }}>
            <Badge letter="Q" label="Question" tone="brand" />
            <p className="mt-2 text-[15px] leading-relaxed text-ink">{detail.question}</p>
            {/* Drawn rather than simply present: it reads as the card being
                written, which is the one flourish this component gets. */}
            <div className="mt-2 h-px w-full origin-left animate-rule bg-gradient-to-r from-brand/50 to-transparent" />
          </section>

          <section className="animate-slide-in" style={{ animationDelay: "190ms" }}>
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

          <section
            className="flex flex-wrap gap-2 border-t border-line pt-4 animate-slide-in"
            style={{ animationDelay: "260ms" }}
          >
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
          <div className="flex items-center gap-2">
            <CopyButton text={asPlainText(detail, report, answer)} />
            <button
              onClick={dismiss}
              className="rounded-lg px-3 py-1.5 text-sm text-muted transition-colors hover:bg-raised hover:text-ink"
            >
              Close
            </button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/** The card for a run this component does not already hold, fetched on demand.
 *
 *  Mounted only once the card is opened, which is what makes the fetch lazy:
 *  hooks run when their component does. The history list deliberately does not
 *  carry summaries -- a list endpoint that ships every answer sends a megabyte
 *  to answer "what did I ask", and most rows are never opened -- so the two
 *  requests happen here, for the one row somebody actually pointed at.
 */
function LazySummaryCard({ researchId, onClose }: { researchId: string; onClose: () => void }) {
  const detail = useResearch(researchId, { live: false });
  const report = useReport(researchId, Boolean(detail.data?.has_report));

  if (detail.isLoading || report.isLoading) {
    return createPortal(
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 backdrop-blur-sm"
        onClick={onClose}
        role="presentation"
      >
        <Spinner label="Loading the summary" />
      </div>,
      document.body,
    );
  }

  // A run can exist and have no report: it failed, or it was cancelled. Saying
  // so is better than an empty card or a spinner that never resolves.
  if (!detail.data || !report.data) {
    return createPortal(
      <div
        className="fixed inset-0 z-50 flex items-center justify-center bg-canvas/80 p-4 backdrop-blur-sm"
        onClick={onClose}
        role="presentation"
      >
        <div
          role="dialog"
          aria-modal="true"
          onClick={(event) => event.stopPropagation()}
          className="w-full max-w-sm rounded-2xl border border-line bg-surface px-6 py-5 text-sm text-muted shadow-2xl"
        >
          This run has no report to summarise.
          <button
            onClick={onClose}
            className="mt-4 block rounded-lg px-3 py-1.5 text-sm text-muted hover:bg-raised hover:text-ink"
          >
            Close
          </button>
        </div>
      </div>,
      document.body,
    );
  }

  return <SummaryCard detail={detail.data} report={report.data} onClose={onClose} />;
}

/** An affordance that opens the card for any run, fetching what it needs.
 *
 *  Used from the history list, where the point is to read an answer without
 *  navigating into the run at all. */
export function SummaryCardTrigger({
  researchId,
  label = "Summary",
}: {
  researchId: string;
  label?: string;
}) {
  const [open, setOpen] = useState(false);

  return (
    <>
      <button
        onClick={(event) => {
          // The row is a link. Without these the click navigates into the run,
          // which is exactly what opening the card is meant to avoid.
          event.preventDefault();
          event.stopPropagation();
          setOpen(true);
        }}
        className="shrink-0 rounded-md border border-line px-2 py-0.5 text-[11px] text-muted transition-colors hover:border-brand/40 hover:text-ink"
      >
        {label}
      </button>
      {open ? <LazySummaryCard researchId={researchId} onClose={() => setOpen(false)} /> : null}
    </>
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
