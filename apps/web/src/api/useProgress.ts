/**
 * Live progress, reconnecting without losing anything.
 *
 * The backend keeps a numbered history of every event and accepts an `after`
 * parameter, which is what makes a dropped connection cost nothing -- but only
 * if the client actually tracks what it last saw. That is this hook's job, and
 * getting it wrong is invisible in development, where sockets do not drop.
 *
 * So the sequence number is held in a ref rather than in state: a reconnect can
 * happen between renders, and reading a stale value would re-request events the
 * user has already seen, making counters climb twice.
 *
 * Reconnection backs off. A server that is down does not become available
 * faster because a browser tab asks it every hundred milliseconds, and a tab
 * left open overnight against a stopped API should not be a load generator.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { eventsUrl } from "./client";
import { TERMINAL_EVENTS, type ProgressEvent } from "./types";

const FIRST_RETRY_MS = 500;
const MAX_RETRY_MS = 10_000;

export type StreamState = "connecting" | "open" | "closed" | "unavailable";

export interface Progress {
  events: ProgressEvent[];
  latest: ProgressEvent | null;
  state: StreamState;
  finished: boolean;
}

export function useProgress(researchId: string, enabled: boolean): Progress {
  const [events, setEvents] = useState<ProgressEvent[]>([]);
  const [state, setState] = useState<StreamState>("connecting");
  const [finished, setFinished] = useState(false);

  // Refs, not state: a reconnect is scheduled from a closure that would
  // otherwise capture whichever values existed when it was created.
  const lastSequence = useRef(0);
  const attempt = useRef(0);
  const socket = useRef<WebSocket | null>(null);
  const timer = useRef<number | null>(null);
  const stopped = useRef(false);

  const connect = useCallback(() => {
    if (stopped.current) return;
    setState("connecting");

    const ws = new WebSocket(eventsUrl(researchId, lastSequence.current));
    socket.current = ws;

    ws.onopen = () => {
      attempt.current = 0;
      setState("open");
    };

    ws.onmessage = (message) => {
      const payload = JSON.parse(message.data as string) as
        | ProgressEvent
        | { kind: "heartbeat"; sequence: number };

      // Heartbeats keep intermediaries from closing an idle connection. They
      // are not events and must not appear in the narration.
      if (payload.kind === "heartbeat") return;

      const event = payload as ProgressEvent;
      if (event.sequence <= lastSequence.current) return; // already seen
      lastSequence.current = event.sequence;

      setEvents((seen) => [...seen, event]);
      if (TERMINAL_EVENTS.has(event.kind)) {
        stopped.current = true;
        setFinished(true);
      }
    };

    ws.onclose = (closed) => {
      socket.current = null;
      if (stopped.current) {
        setState("closed");
        return;
      }

      // 1013 is the server saying streaming is not available at all. Retrying
      // that is asking the same question until the deployment changes, so the
      // UI is told to fall back to polling instead.
      if (closed.code === 1013) {
        setState("unavailable");
        return;
      }

      setState("closed");
      const delay = Math.min(FIRST_RETRY_MS * 2 ** attempt.current, MAX_RETRY_MS);
      attempt.current += 1;
      timer.current = window.setTimeout(connect, delay);
    };

    ws.onerror = () => ws.close();
  }, [researchId]);

  useEffect(() => {
    if (!enabled) return;

    stopped.current = false;
    lastSequence.current = 0;
    attempt.current = 0;
    setEvents([]);
    setFinished(false);
    connect();

    return () => {
      // Torn down on unmount, always. A socket left open by a component that
      // is gone holds a connection on the server for a page nobody is looking
      // at, and one per navigation adds up quickly.
      stopped.current = true;
      if (timer.current !== null) window.clearTimeout(timer.current);
      socket.current?.close();
      socket.current = null;
    };
  }, [connect, enabled]);

  return {
    events,
    latest: events.length ? (events[events.length - 1] as ProgressEvent) : null,
    state,
    finished,
  };
}
