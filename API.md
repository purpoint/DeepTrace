# API

FastAPI, async end to end. An interactive schema is served at `/docs` and the
OpenAPI document at `/openapi.json` — both generated from the same Pydantic
models the routes validate against, so they cannot drift from the
implementation.

This describes the parts a generated schema does not: what the errors mean, why
there are two tokens, and how a long-running job is observed.

**Base URL.** The API is mounted at the root. Behind the bundled nginx it is
proxied under `/api` **with the prefix stripped**, so the service itself always
sees `/auth/login`, never `/api/auth/login`. A client addressing the API
directly must not add the prefix.

---

## Authentication

### Two tokens, because they are asked different questions

| | Access token | Refresh token |
|---|---|---|
| Answers | "who is this request from?" | "is this session still allowed?" |
| Lifetime | 15 minutes | 14 days |
| Verified by | signature alone, no lookup | a row in the database |
| Revocable | no | yes, individually |
| Stored by the client | memory only | `localStorage` |

An access token is verified without touching the database, which is what makes
every authenticated request cheap — and is exactly why it cannot be revoked.
Fifteen minutes is the exposure window and the price of that. A refresh token is
recorded server-side, so `logout-everywhere` ends sessions immediately.

Refresh tokens **rotate**: using one invalidates it and issues another. A token
presented twice means one of the two holders is not the user — and there is no
way to tell which — so every session for that account is destroyed and the person
must sign in with a password only they know.

With one deliberate exception. Two refreshes can race legitimately: a client with
two tabs, or a component that mounts twice. Both present the same token, one
wins, and the loser looks exactly like a thief. So a consumed token is remembered
for a short window, and a second presentation inside it is **refused rather than
treated as theft**. The cost is stated rather than hidden: a thief using a stolen
token inside that window escapes detection. Thirty seconds of that is worth not
signing people out of their own accounts for opening a second tab.

### Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/auth/register` | Create an account · **201** |
| `POST` | `/auth/login` | Exchange a password for a token pair |
| `POST` | `/auth/refresh` | Renew a session, rotating the refresh token |
| `POST` | `/auth/logout` | End this session · **204** |
| `POST` | `/auth/logout-everywhere` | End every session for the account |
| `GET` | `/auth/me` | The signed-in account |
| `POST` | `/auth/ws-ticket` | A single-use, 30-second credential for a WebSocket |

```bash
curl -X POST https://your-api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"email":"you@example.com","password":"…"}'
```

```json
{ "access_token": "…", "refresh_token": "…", "token_type": "Bearer", "expires_in": 900 }
```

`expires_in` is seconds, not an absolute time. An absolute expiry requires the
client's clock to agree with the server's, and a browser ten minutes fast would
refresh constantly or not at all.

**Registration discloses whether an address is already registered.** It has to
refuse a duplicate, so the enumeration is bounded by the rate limit rather than
denied. Login does not disclose it: same status, same code, same sentence, and a
dummy hash so the timing matches.

### The WebSocket ticket

A browser cannot set a header when opening a WebSocket, and a token in a query
string is written to every access log in the path. So `/auth/ws-ticket` mints a
credential good for **one connection and thirty seconds** — worthless by the time
anyone reads the log it landed in.

---

## Research

Every endpoint below requires `Authorization: Bearer <access_token>`, and every
one is **scoped to the account that owns the run**. Ownership is enforced in the
query rather than checked afterwards, so a route cannot forget it.

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/research` | Ask a question · **202** |
| `GET` | `/research` | This account's runs, newest first |
| `GET` | `/research/{id}` | One run: status, counts, job state |
| `GET` | `/research/{id}/report` | The document, markdown and structured |
| `GET` | `/research/{id}/claims` | Each claim, its verdict and its evidence |
| `GET` | `/research/{id}/evidence` | Each passage, and whether it was found in its page |
| `GET` | `/research/{id}/sources` | Pages retrieved, with quality scores |
| `GET` | `/research/{id}/trace` | Every model and tool call the run made |
| `GET` | `/research/{id}/cost` | What it cost, by agent |
| `POST` | `/research/{id}/cancel` | Ask a running job to stop |
| `WS` | `/research/{id}/events` | Live progress, replayable |

### Submitting

```bash
curl -X POST https://your-api/research \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"question":"What does the CAP theorem actually constrain?","depth":"quick"}'
```

```json
{ "research_id": "res_1b4f…", "status": "queued" }
```

**202, not 201.** Nothing has been created yet except the intention. The research
does not exist until a worker has run it, and a client following a `Location`
header would find nothing there.

`depth` is `quick` · `standard` · `deep`, and selects a hard budget:

| depth | tasks | sources | verify loops | roughly |
|---|---|---|---|---|
| `quick` | 3 | 8 | 0 | 2 minutes |
| `standard` | 6 | 20 | 1 | 5 minutes |
| `deep` | 12 | 50 | 3 | 15 minutes or more |

### Watching a run

```
wss://your-api/research/{id}/events?ticket=<ticket>&after=<sequence>
```

Events are **numbered**, and a history is kept alongside the live stream. A
client that reconnects sends the last sequence it saw in `after` and receives
everything since — so a dropped socket loses nothing. Omit `after` to replay from
the beginning.

Polling `GET /research/{id}` works too, and is what the client falls back to when
no socket is available.

---

## Errors

One envelope, everywhere:

```json
{
  "error": {
    "code": "not_found",
    "message": "No report for research with id res_abc.",
    "details": { "id": "res_abc" },
    "reference": null
  }
}
```

**`code` is what a client branches on.** A client that has to match on prose is a
client that breaks the first time the wording improves.

| Code | HTTP | Means |
|---|---|---|
| `invalid_request` | 422 | The body or parameters failed validation — see `details.fields` |
| `unauthenticated` | 401 | No usable credential |
| `token_expired` | 401 | A credential that *was* valid — refresh and retry |
| `not_found` | 404 | No such run, or not this account's |
| `conflict` | 409 | The state does not allow it |
| `rate_limited` | 429 | Too many requests |
| `unavailable` | 503 | A dependency is down |
| `internal` | 500 | Unhandled — carries a `reference`, and nothing else |

The codes are **deliberately coarse**. One per internal failure would make the
error surface a mirror of the implementation, and every refactor a breaking
change.

`token_expired` is split from `unauthenticated` because it is the one
authentication failure a client should handle silently — refresh and retry
rather than show a sign-in screen. It discloses nothing: the expiry is written in
the token the client already holds.

`internal` carries a `reference` and no detail. Nothing about the failure escapes
the server; the reference is what correlates a user's report with a log line.

---

## Rate limits

Two policies, because two different things are being protected.

| | Limit | Window | Counted per |
|---|---|---|---|
| Authentication | 10 | 15 minutes | client address |
| Submitting research | 20 | 1 hour | user |

Authentication is protected from **guessing**, so it is counted per address —
the attacker chooses the account. Research is protected from **spending**, so it
is counted per user — that is whose money it is.

The client address is the socket's peer, not `X-Forwarded-For`, which is written
by the client and rewritten by every proxy in the path. Behind a proxy the API
must be started with `--forwarded-allow-ips` naming the trusted hop; both
supplied deployments do this. Trusting the header unconditionally would let a
caller pick a fresh bucket per request, which is not a rate limit.

**A limit this API does not enforce.** The binding constraint on a deployed
instance is the model provider's: roughly 20 requests a day on the free tier,
about seven per run. The per-user submit limit does not help there, because the
provider counts across all users at once.

---

## Health

```bash
curl https://your-api/health
```

```json
{ "status": "ok", "database": true, "queue": true, "version": "0.1.0" }
```

**Always 200**, with each dependency reported separately in the body. A health
check that refuses to answer without PostgreSQL cannot tell you *that* PostgreSQL
is what is missing. Container health here means "can serve"; whether it can serve
*usefully* is what the body says.
