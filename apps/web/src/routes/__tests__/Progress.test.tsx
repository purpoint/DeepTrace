/**
 * Tests for the progress screen.
 *
 * This is the one screen that had never been opened. Everything else in the
 * client was looked at while it was built; Progress needs a run in flight to
 * show anything, and a run costs real quota, so it was written and shipped and
 * never seen.
 *
 * Reading it found what looking would have. For a finished run the hook below
 * it did not connect at all, so the screen rendered seven unticked stages under
 * the heading "Researching", subtitled "Live", above a spinner reading "Waiting
 * for the worker to pick this up" -- for a run that had completed days earlier.
 * The commit that made tabs addressable turned that into a shareable URL.
 *
 * So what is pinned here is the distinction the screen has to draw: watching
 * work happen, and reading back a recording of work that already happened.
 */

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ProgressView } from "../Progress";
import { api } from "../../api/client";
import type { ProgressEvent, ResearchDetail } from "../../api/types";

class FakeSocket {
  static live: FakeSocket[] = [];
  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.live.push(this);
    queueMicrotask(() => this.onopen?.());
  }

  deliver(event: Partial<ProgressEvent>): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  drop(code = 1000): void {
    this.closed = true;
    this.onclose?.({ code });
  }

  close(): void {
    this.closed = true;
    this.onclose?.({ code: 1000 });
  }
}

const detail = (status: string): ResearchDetail =>
  ({
    research_id: "res_abc123",
    question: "Compare Kafka and RabbitMQ for high-scale microservices.",
    depth: "standard",
    status,
    created_at: new Date().toISOString(),
    sources: 8,
    evidence: 24,
    claims: 12,
    has_report: true,
    job: null,
  }) as unknown as ResearchDetail;

const event = (sequence: number, kind: string, message: string): Partial<ProgressEvent> => ({
  version: 1,
  sequence,
  research_id: "res_abc123",
  kind: kind as ProgressEvent["kind"],
  message,
  data: {},
  at: new Date().toISOString(),
});

function draw(status: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <ProgressView detail={detail(status)} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  FakeSocket.live = [];
  vi.spyOn(api, "wsTicket").mockResolvedValue({ ticket: "t-1", expires_in: 30 });
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("location", { protocol: "http:", host: "localhost:5173" });
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

describe("the progress screen, on a run that is going", () => {
  it("says it is live and offers to stop the run", async () => {
    draw("researching");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    expect(screen.getByText("Researching")).toBeTruthy();
    expect(screen.getByText("Live")).toBeTruthy();
    expect(screen.getByText("Stop")).toBeTruthy();
  });

  it("waits on the worker when nothing has happened yet", async () => {
    draw("queued");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    expect(screen.getByText(/Waiting for the worker/)).toBeTruthy();
  });
});

describe("the progress screen, on a run that has finished", () => {
  it("replays what happened rather than claiming to be live", async () => {
    draw("completed");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.deliver(event(1, "started", "Research started"));
      FakeSocket.live[0]!.deliver(event(2, "report_ready", "Report written"));
      FakeSocket.live[0]!.drop(1000);
    });

    expect(screen.getByText("How this run went")).toBeTruthy();
    expect(screen.getByText("Replayed from what was recorded")).toBeTruthy();
    expect(screen.getByText("What it did")).toBeTruthy();
    expect(screen.getByText("Report written")).toBeTruthy();
    // The word that was wrong. A finished run is not live, and the screen said
    // so for as long as it existed.
    expect(screen.queryByText("Live")).toBeNull();
  });

  it("ticks every stage once the replay reaches the report", async () => {
    // The stage list is derived from the events, so a replay that arrives is
    // also what makes the seven steps read as done. Without the replay they
    // were all grey, which said the run had not started.
    draw("completed");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => FakeSocket.live[0]!.deliver(event(1, "report_ready", "Report written")));

    expect(screen.getAllByText("✓")).toHaveLength(7);
  });

  it("never offers to stop a run that is already over", async () => {
    draw("completed");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    expect(screen.queryByText("Stop")).toBeNull();
  });

  it("says the narration has expired rather than spinning", async () => {
    // Progress is a capped list per run, so an old run's events are genuinely
    // gone. The screen used to answer that with a spinner waiting for a worker
    // which had finished with this run long before.
    draw("completed");
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => FakeSocket.live[0]!.drop(1000));

    await waitFor(() => expect(screen.getByText(/no longer recorded/)).toBeTruthy());
    expect(screen.queryByText(/Waiting for the worker/)).toBeNull();
  });
});
