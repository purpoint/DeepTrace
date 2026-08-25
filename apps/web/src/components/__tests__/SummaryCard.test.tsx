/**
 * Tests for the summary card.
 *
 * Two things are defended here. That the card never becomes a bare answer --
 * it must carry the support behind it, because a confident sentence with
 * nothing attached is the exact failure this product exists to prevent. And
 * that it renders through a portal, which is not a stylistic preference: the
 * first version was present in the DOM, fully opaque, correctly sized, and
 * 2263px below the fold.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { SummaryCard, asPlainText, swipeOutcome, trimTrailingCitations } from "../SummaryCard";
import type { ReportView, ResearchDetail } from "../../api/types";

const detail = {
  research_id: "res_abc123",
  question: "How does the TCP three-way handshake establish a connection?",
  normalized_question: "How does the TCP three-way handshake establish a connection?",
  research_type: "explanation",
  depth: "quick",
  status: "completed",
  created_at: new Date().toISOString(),
  completed_at: null,
  error: null,
  sources: 8,
  evidence: 11,
  claims: 6,
  has_report: true,
  job: null,
} as unknown as ResearchDetail;

const report = (over: Partial<ReportView> = {}) =>
  ({
    research_id: "res_abc123",
    title: "TCP",
    markdown: "# TCP",
    citations: 11,
    fully_cited: true,
    structured: {
      title: "TCP",
      question: detail.question,
      citations: [],
      sections: [
        { kind: "summary", body: "It uses SYN, SYN-ACK, ACK [1, 2, 3].", citation_numbers: [] },
      ],
    },
    ...over,
  }) as unknown as ReportView;

describe("trimming the citation run", () => {
  it("removes a trailing cluster", () => {
    expect(trimTrailingCitations("It uses SYN [1, 2, 3, 4, 5].")).toBe("It uses SYN.");
  });

  it("leaves markers inside the sentence alone", () => {
    /** Only the trailing run is noise. A marker mid-sentence is attached to the
     *  clause it supports, and removing it would take the provenance with it. */
    const text = "Kafka orders records [3] within one partition [4, 5].";

    expect(trimTrailingCitations(text)).toBe("Kafka orders records [3] within one partition.");
  });

  it("leaves a sentence with no markers untouched", () => {
    expect(trimTrailingCitations("It uses a handshake.")).toBe("It uses a handshake.");
  });
});

describe("the card", () => {
  it("renders into document.body, not in place", () => {
    /** The bug this exists for. `position: fixed` is only fixed to the viewport
     *  when no ancestor has a transform; the tab content sits inside an
     *  `animate-slide-in` wrapper, so the backdrop resolved against a
     *  4502px-tall element and the card landed far below the fold -- visible to
     *  every assertion and to nobody looking at the screen. */
    const { container } = render(
      <SummaryCard detail={detail} report={report()} onClose={vi.fn()} />,
    );

    expect(container).toBeEmptyDOMElement();
    expect(screen.getByRole("dialog")).toBeInTheDocument();
  });

  it("shows the question and the answer", () => {
    render(<SummaryCard detail={detail} report={report()} onClose={vi.fn()} />);

    expect(screen.getByText(detail.question)).toBeInTheDocument();
    expect(screen.getByText("It uses SYN, SYN-ACK, ACK.")).toBeInTheDocument();
  });

  it("always carries the support behind the answer", () => {
    /** The card must never be a bare assertion. If it cannot say what backs the
     *  sentence, it has become the thing this system is built to refuse. */
    render(<SummaryCard detail={detail} report={report()} onClose={vi.fn()} />);

    expect(screen.getByText("6 claims verified")).toBeInTheDocument();
    expect(screen.getByText("11 citations")).toBeInTheDocument();
    expect(screen.getByText("8 sources")).toBeInTheDocument();
    expect(screen.getByText("explanation")).toBeInTheDocument();
  });

  it("says so when citations did not resolve", () => {
    /** Bad news is on the face of the card, not in a tab the reader has to go
     *  looking for. */
    render(
      <SummaryCard detail={detail} report={report({ fully_cited: false })} onClose={vi.fn()} />,
    );

    expect(screen.getByText("some citations unresolved")).toBeInTheDocument();
  });

  it("reports an absent summary rather than rendering blank", () => {
    const empty = report();
    empty.structured.sections = [];

    render(<SummaryCard detail={detail} report={empty} onClose={vi.fn()} />);

    expect(screen.getByText("This run produced no summary section.")).toBeInTheDocument();
  });

  it("closes on Escape, after playing the exit", async () => {
    /** Two halves, and both matter.
     *
     *  The card must start leaving immediately, or Escape feels ignored. And
     *  it must not unmount until the exit animation has run, or the card
     *  vanishes on the frame the state changes -- an animated entrance
     *  followed by an instant disappearance reads worse than no animation at
     *  all, because the eye notices the asymmetry even when the viewer could
     *  not name it. */
    const onClose = vi.fn();
    render(<SummaryCard detail={detail} report={report()} onClose={onClose} />);

    fireEvent.keyDown(document, { key: "Escape" });

    expect(screen.getByRole("dialog").className).toContain("animate-card-out");
    expect(onClose).not.toHaveBeenCalled();

    await waitFor(() => expect(onClose).toHaveBeenCalled());
  });

  it("does not stack dismissals from a burst of native events", () => {
    /** A held Escape key, dispatched the way the browser dispatches it.
     *
     *  Deliberately raw `dispatchEvent` rather than `fireEvent`, and that is
     *  the whole point of the test. `fireEvent` wraps each event in `act()`,
     *  which flushes a re-render in between, so the listener is rebuilt with
     *  fresh state and a guard reading `leaving` from the closure appears to
     *  work. Real key repeat does not pause for React: every event in the
     *  burst hits the same closure, all four see `leaving === false`, and four
     *  unmounts are queued.
     *
     *  Written with fireEvent first, and it passed against the broken guard.
     */
    const onClose = vi.fn();
    render(<SummaryCard detail={detail} report={report()} onClose={onClose} />);

    for (let i = 0; i < 4; i += 1) {
      document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    }

    return waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
  });

  it("locks the page behind it and gives scrolling back", () => {
    /** A modal the document can scroll underneath feels like a sticker rather
     *  than a layer. */
    const { unmount } = render(
      <SummaryCard detail={detail} report={report()} onClose={vi.fn()} />,
    );
    expect(document.body.style.overflow).toBe("hidden");

    unmount();

    expect(document.body.style.overflow).not.toBe("hidden");
  });
});

describe("the plain-text form", () => {
  it("carries the support and the run id with it", () => {
    /** A question and an answer pasted into a chat with nothing else is the
     *  uncited answer this card exists to avoid, except now it has escaped the
     *  product and nobody can tell where it came from. */
    const text = asPlainText(detail, report(), "It uses SYN, SYN-ACK, ACK.");

    expect(text).toContain("Q: How does the TCP three-way handshake");
    expect(text).toContain("A: It uses SYN, SYN-ACK, ACK.");
    expect(text).toContain("6 claims verified");
    expect(text).toContain("11 citations");
    expect(text).toContain("res_abc123");
  });

  it("says when citations did not resolve", () => {
    const text = asPlainText(detail, report({ fully_cited: false }), "Something.");

    expect(text).toContain("some citations unresolved");
  });

  it("does not pretend there was an answer when there was none", () => {
    const text = asPlainText(detail, report(), null);

    expect(text).toContain("no summary section");
  });
});

describe("copying", () => {
  it("writes the card to the clipboard", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });

    render(<SummaryCard detail={detail} report={report()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(writeText).toHaveBeenCalledOnce();
    expect(String(writeText.mock.calls[0]?.[0])).toContain("Q: How does the TCP");
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
  });

  it("says so when the clipboard refuses", async () => {
    /** Unavailable on an insecure origin and in older browsers. A button that
     *  silently does nothing is worse than one that admits it failed. */
    Object.assign(navigator, {
      clipboard: { writeText: vi.fn().mockRejectedValue(new Error("denied")) },
    });

    render(<SummaryCard detail={detail} report={report()} onClose={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));

    expect(await screen.findByRole("button", { name: "Could not copy" })).toBeInTheDocument();
  });
});

describe("swipe to dismiss", () => {
  /**
   * Tested through the decision function rather than by firing pointer events.
   *
   * jsdom's PointerEvent carries neither `clientY` nor `pointerType`, so
   * dispatching one at the card exercises nothing at all. The first version of
   * this block did exactly that, and three of its four tests passed -- they
   * asserted the card had NOT closed, which is trivially true when the handler
   * receives undefined coordinates. Green, and testing the absence of a
   * feature.
   *
   * The React wiring is three handlers and a transform, verified in a real
   * browser. The decisions live here.
   */

  it("dismisses on a long enough downward swipe", () => {
    expect(
      swipeOutcome({ pointerType: "touch", startY: 100, endY: 300, contentScrolled: false }),
    ).toBe("dismiss");
  });

  it("springs back from a short one", () => {
    /** A short drag is usually the start of a scroll, or a thumb resting.
     *  Throwing the card away on either would make it feel unstable. */
    expect(
      swipeOutcome({ pointerType: "touch", startY: 100, endY: 140, contentScrolled: false }),
    ).toBe("spring-back");
  });

  it("springs back from an upward swipe", () => {
    expect(
      swipeOutcome({ pointerType: "touch", startY: 300, endY: 100, contentScrolled: false }),
    ).toBe("spring-back");
  });

  it("ignores a mouse drag", () => {
    /** Touch only, so a drag across the answer selects text instead of
     *  throwing the card away. */
    expect(
      swipeOutcome({ pointerType: "mouse", startY: 100, endY: 400, contentScrolled: false }),
    ).toBe("ignore");
  });

  it("ignores a swipe that began while the answer was scrolled", () => {
    /** The standard way this gesture is got wrong: a swipe meant to scroll a
     *  long answer takes the whole card with it. */
    expect(
      swipeOutcome({ pointerType: "touch", startY: 400, endY: 700, contentScrolled: true }),
    ).toBe("ignore");
  });
});
