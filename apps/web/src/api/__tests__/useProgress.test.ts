/**
 * Tests for the progress socket.
 *
 * The backend keeps a numbered history and accepts an `after` parameter, so a
 * reconnect can lose nothing -- but only if the client tracks what it last saw
 * and asks for the rest. That bookkeeping is invisible in development, where
 * sockets do not drop, so it is worth pinning here.
 */

import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useProgress } from "../useProgress";
import type { ProgressEvent } from "../types";

/** A WebSocket stand-in that records the URLs it was opened with.
 *
 *  The URL is the assertion: it carries the `after` value, which is the entire
 *  reconnection contract. */
class FakeSocket {
  static opened: string[] = [];
  static live: FakeSocket[] = [];
  /** Whether a connection reaches `open`. A server that is down fails before
   *  opening, which is the case where backoff has to escalate. */
  static opensSuccessfully = true;

  onopen: (() => void) | null = null;
  onmessage: ((event: { data: string }) => void) | null = null;
  onclose: ((event: { code: number }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;

  constructor(readonly url: string) {
    FakeSocket.opened.push(url);
    FakeSocket.live.push(this);
    if (FakeSocket.opensSuccessfully) queueMicrotask(() => this.onopen?.());
  }

  deliver(event: Partial<ProgressEvent>): void {
    this.onmessage?.({ data: JSON.stringify(event) });
  }

  drop(code = 1006): void {
    this.closed = true;
    this.onclose?.({ code });
  }

  close(): void {
    this.closed = true;
  }
}

const event = (sequence: number, kind = "stage"): Partial<ProgressEvent> => ({
  version: 1,
  sequence,
  research_id: "res_1",
  kind: kind as ProgressEvent["kind"],
  message: `event ${sequence}`,
  data: {},
  at: new Date().toISOString(),
});

beforeEach(() => {
  FakeSocket.opened = [];
  FakeSocket.live = [];
  FakeSocket.opensSuccessfully = true;
  vi.stubGlobal("WebSocket", FakeSocket);
  vi.stubGlobal("location", { protocol: "http:", host: "localhost:5173" });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe("useProgress", () => {
  it("starts from the beginning of the stream", async () => {
    renderHook(() => useProgress("res_1", true));

    await waitFor(() => expect(FakeSocket.opened).toHaveLength(1));
    expect(FakeSocket.opened[0]).toContain("after=0");
  });

  it("collects events in order", async () => {
    const { result } = renderHook(() => useProgress("res_1", true));
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.deliver(event(1));
      FakeSocket.live[0]!.deliver(event(2));
    });

    expect(result.current.events.map((e) => e.sequence)).toEqual([1, 2]);
  });

  it("asks only for what it has not seen when it reconnects", async () => {
    // The whole contract. Reconnecting with after=0 would replay events the
    // user already watched, and every counter in the UI would climb twice.
    vi.useFakeTimers();
    renderHook(() => useProgress("res_1", true));
    await vi.waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.deliver(event(1));
      FakeSocket.live[0]!.deliver(event(2));
      FakeSocket.live[0]!.deliver(event(3));
      FakeSocket.live[0]!.drop();
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });

    expect(FakeSocket.opened).toHaveLength(2);
    expect(FakeSocket.opened[1]).toContain("after=3");
  });

  it("ignores an event it has already seen", async () => {
    // Replay and live delivery overlap by design, so the same event can arrive
    // by both paths. Counting it twice is what that would look like.
    const { result } = renderHook(() => useProgress("res_1", true));
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.deliver(event(1));
      FakeSocket.live[0]!.deliver(event(1));
    });

    expect(result.current.events).toHaveLength(1);
  });

  it("ignores heartbeats", async () => {
    const { result } = renderHook(() => useProgress("res_1", true));
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.onmessage?.({
        data: JSON.stringify({ kind: "heartbeat", sequence: 0 }),
      });
    });

    expect(result.current.events).toHaveLength(0);
  });

  it("stops reconnecting once the run has finished", async () => {
    vi.useFakeTimers();
    const { result } = renderHook(() => useProgress("res_1", true));
    await vi.waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => {
      FakeSocket.live[0]!.deliver(event(1, "completed"));
      FakeSocket.live[0]!.drop(1000);
    });

    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(result.current.finished).toBe(true);
    expect(FakeSocket.opened).toHaveLength(1);
  });

  it("gives up when the server says streaming is unavailable", async () => {
    // 1013 means the deployment has no event stream. Retrying asks the same
    // question until something is redeployed, so the UI falls back to polling.
    vi.useFakeTimers();
    const { result } = renderHook(() => useProgress("res_1", true));
    await vi.waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => FakeSocket.live[0]!.drop(1013));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(30_000);
    });

    expect(result.current.state).toBe("unavailable");
    expect(FakeSocket.opened).toHaveLength(1);
  });

  it("resets its backoff once a connection succeeds", async () => {
    // A connection that opened means the server is there. The next failure is
    // a fresh problem and should be retried promptly rather than inheriting
    // the patience earned by an earlier outage.
    vi.useFakeTimers();
    renderHook(() => useProgress("res_1", true));
    await vi.waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => FakeSocket.live[0]!.drop());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(FakeSocket.opened).toHaveLength(2);

    // The second socket opened successfully before dropping, so the wait is
    // the first delay again rather than double it.
    act(() => FakeSocket.live[1]!.drop());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(FakeSocket.opened).toHaveLength(3);
  });

  it("waits longer each time a connection cannot be established", async () => {
    // A tab left open overnight against a stopped API should not be a load
    // generator.
    vi.useFakeTimers();
    FakeSocket.opensSuccessfully = false;
    renderHook(() => useProgress("res_1", true));
    await vi.waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    act(() => FakeSocket.live[0]!.drop());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(FakeSocket.opened).toHaveLength(2); // waited ~500ms

    act(() => FakeSocket.live[1]!.drop());
    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(FakeSocket.opened).toHaveLength(2); // ~1000ms this time, so not yet

    await act(async () => {
      await vi.advanceTimersByTimeAsync(600);
    });
    expect(FakeSocket.opened).toHaveLength(3);
  });

  it("closes the socket when the component goes away", async () => {
    // One leaked socket per navigation adds up on the server for pages nobody
    // is looking at.
    const { unmount } = renderHook(() => useProgress("res_1", true));
    await waitFor(() => expect(FakeSocket.live).toHaveLength(1));

    unmount();

    expect(FakeSocket.live[0]!.closed).toBe(true);
  });

  it("does not connect at all when disabled", () => {
    renderHook(() => useProgress("res_1", false));

    expect(FakeSocket.opened).toHaveLength(0);
  });
});
