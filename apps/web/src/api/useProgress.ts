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
 *
 * Each connection needs its own ticket, fetched immediately before it opens. A
 * browser cannot attach a header to a WebSocket, so the credential travels in
 * the URL -- and a ticket is what makes that survivable: thirty seconds long
 * and destroyed by first use, so the copy left in an access log is worthless.
 * The consequence is that a reconnect costs one HTTP request before the socket,
 * which is the right price for not putting a fifteen-minute token in a URL.
 */

import { useCallback, useEffect, useRef, useState } from "react";

import { api, eventsUrl } from "./client";
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

  const scheduleRetry = useCallback(() => {
    if (stopped.current) return;
    const delay = Math.min(FIRST_RETRY_MS * 2 ** attempt.current, MAX_RETRY_MS);
    attempt.current += 1;
    timer.current = window.setTimeout(() => void connectRef.current(), delay);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A ref, because connect and scheduleRetry call each other and a plain
  // reference would capture whichever one was defined first -- which is a
  // reconnect loop that reconnects with stale state, and it only shows up when
  // a socket actually drops.
  const connectRef = useRef<() => Promise<void>>(async () => {});

  const connect = useCallback(async () => {
    if (stopped.current) return;
    setState("connecting");

    let ticket: string;
    try {
      ticket = (await api.wsTicket()).ticket;
    } catch {
      // No ticket, no socket. Treated like any other failed connection: back
      // off and try again, because the usual cause is a token that is being
      // refreshed or an API that is restarting, and both resolve on their own.
      scheduleRetry();
      return;
    }

    // The component may have unmounted while the ticket was in flight. Opening
    // a socket now would leak one that nothing will ever close.
    if (stopped.current) return;

    const ws = new WebSocket(eventsUrl(researchId, ticket, lastSequence.current));
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
      scheduleRetry();
    };

    ws.onerror = () => ws.close();
  }, [researchId]);

  connectRef.current = connect;

  useEffect(() => {
    if (!enabled) return;

    stopped.current = false;
    lastSequence.current = 0;
    attempt.current = 0;
    setEvents([]);
    setFinished(false);
    void connect();

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
