# ADR-004: WebSockets for research progress, with numbered replay

**Status:** Accepted · **Date:** 2026-08

## Context

A run takes two to fifteen minutes. Without progress, a user watches a spinner
and assumes the system is broken.

Polling is simpler. Server-sent events are simpler still, and one-directional,
which is all this needs.

## Decision

A WebSocket, carrying **numbered** events, with a replayable history.

## Consequences

**The numbering is the substance, not the socket.** A stream that only pushes
live events loses whatever happened while the socket was down. A client
reconnecting sends the last sequence it saw and receives everything after it, so
a dropped connection costs nothing.

That failure is invisible in development, where sockets do not drop. It is the
reason a plain live stream was not enough.

**Authentication needed its own answer.** A browser cannot set a header when
opening a WebSocket. Sending the access token in the query string would write it
to every access log in the path. So the API mints a **single-use ticket, valid
for thirty seconds** — worthless by the time anyone reads the log it landed in.

**Consequences accepted.** A WebSocket needs proxy configuration that ordinary
requests do not: upgrade headers, and a read timeout longer than a research run.
nginx's default of sixty seconds would close a healthy stream mid-run — which
fails while every other endpoint works, making it a confusing failure rather
than an obvious one. Both are configured and both are tested.

It also cannot be proxied by a static host, which constrains deployment: a split
frontend must address the API directly for the stream, and its content-security
policy must allow `wss:` as well as `https:`.
