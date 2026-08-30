# Troubleshooting

Every failure below happened. None is invented, and where the error message
pointed somewhere unhelpful, that is recorded too — because the misleading half
is usually the expensive half.

Organised by what you see, not by what it turns out to be.

---

## Starting up

### `TypeError: connect() got an unexpected keyword argument 'sslmode'`

Or `'channel_binding'`, or another libpq parameter.

**Cause.** A managed Postgres — Neon, Supabase, RDS — hands out a libpq
connection string ending in `?sslmode=require&channel_binding=require`. psycopg
reads those; **asyncpg does not**. The error names a keyword argument rather than
SSL, which sends you into the wrong file entirely.

**Fix.** Already handled: `normalise_database_url` renames what has an asyncpg
equivalent and drops what does not. If a *new* parameter appears, the error names
it and it is one line in `_DROPPED_FOR_ASYNCPG` or `_RENAMED_FOR_ASYNCPG`.

Fixing one parameter at a time buys exactly one deploy before the next fails
identically. Handle the family.

### `socket.gaierror: [Errno -2] Name or service not known`

During `applying migrations`.

**Cause.** The database hostname does not resolve. Nine times in ten the database
is **suspended or deleted**, not misconfigured — a suspended managed instance has
nothing behind its internal hostname.

**Check.** Is the database running? Then is `DATABASE_URL` actually populated —
a blueprint that *associated* an existing database rather than creating one may
not have bound the reference.

### `SettingsError: error parsing value for field "cors_origins"` / `Expecting value: line 1 column 1`

**Cause.** `CORS_ORIGINS=` — empty. pydantic-settings JSON-decodes list fields
from the environment, and an empty string is not valid JSON. The message names
neither the variable nor the reason.

**Fix.** Handled: empty means no origins, and a comma-separated list works, which
is how a person writes a list into an environment variable.

### `ImportError: no pq wrapper available`

Worker only; the API starts fine.

**Cause.** psycopg3 arrives with LangGraph's checkpointer and does not bundle
libpq. Only the worker opens a checkpointer — so **the queue accepts research
that nothing will ever run**, which looks like a hang rather than a crash.

**Fix.** `libpq5` in the image. It works on a development machine because
Homebrew's postgres installed libpq years ago.

### `Both JWT_SECRET and JWT_SECRET_FILE are set`

**Cause.** Exactly what it says, and it is deliberate. Quietly preferring one
would mean an operator mid-migration believes they rotated a credential that is
still the old one.

**A subtler version:** in Docker Compose, `KEY:` with *no value* does not clear a
variable — it means "pass this through from the environment", and Compose's
environment includes the project's `.env`. That is how a deployment overlay once
injected a developer's local `DATABASE_URL` into a production container. Use
`KEY: ""`.

### `APP_ENV is production but N required credential(s) are missing`

Working as intended. Production refuses to start without `JWT_SECRET`,
`DATABASE_URL`, `GOOGLE_API_KEY` and `TAVILY_API_KEY`, and names all of them at
once so three missing credentials cost one restart rather than three.

---

## Building and deploying

### The build fails in about eleven seconds: `Exited with status 1 while building your code`

**Cause.** A `COPY` whose source is excluded by `.dockerignore` finds nothing.
The error mentions neither `.dockerignore` nor the directory, and eleven seconds
is too short to look like a real build failure.

**Check.** For every path the Dockerfile copies, is it in the build context? A
test now checks this for the deployment entrypoint.

### `nginx: [emerg] host not found in upstream "api:8000"`

**Cause.** `nginx -t` does not only parse — it **resolves every host named in an
`upstream` block**. A configuration naming a compose service cannot be checked
outside compose. The name is correct; the network is absent.

**Fix.** For a syntax check only, make the name resolve: `docker run --add-host
api:127.0.0.1 … nginx -t`. Nothing connects.

### The service reports unhealthy while serving every request correctly

Two distinct causes, both seen:

**`pgrep: not found`** — the worker health check calls it and it is not in
`python:3.13-slim`. Exit 127 every time, so a perfectly healthy worker is
permanently unhealthy. `procps` fixes it.

**`localhost` inside a container resolves to `::1` first**, and nginx's `listen
8080` binds IPv4 only. curl quietly falls back to IPv4; **BusyBox wget does
not** — which is why a curl-based check passed and a wget-based one could never
pass. Use `127.0.0.1`.

A container that is fine and reports otherwise is the same class of failure as
one that is broken and reports healthy.

### A health path returns a redirect instead of 200

**Cause.** In nginx, a **server-level `return` runs in the rewrite phase, before
location matching** — so it beats even an exact `location = /healthz`. The health
path becomes dead configuration that reads as though it works.

**Fix.** Put the redirect in `location /`.

---

## Running research

### The run finishes with sources and evidence but no report

Status `partial`, and the `error` field says why.

**Cause.** Usually the strong tier failed. A failed analysis is deliberately *not*
a failed run — the evidence is collected, verified and stored, and worth more than
the conclusions drawn from it — so the run keeps what it has.

**Check.** `LLM_MODEL_STRONG`. `gemini-3.7-flash` stopped answering on
2026-08-25: it accepts a request and never replies, returning 504 after a
300-second deadline. A trivial prompt is enough to test it.

Runs recorded before this was fixed still read `completed`, because status is
computed at save time and history is not rewritten.

### `LLMRateLimitError: Quota exceeded … limit: 20`

**Cause.** The free tier allows **20 model requests per day per model**, and one
quick-depth run spends about seven. That is roughly six runs a day for a whole
deployment, shared across everyone using it.

It resets at **midnight Pacific**, not UTC — the calendar date rolling over
locally is not the reset.

**Note.** The per-user submit limit does not help: the provider counts across all
users at once.

### `LLMServerError: 503` or `504 Deadline expired`

**Cause.** The provider, not you. A 503 is capacity; a 504 after the full
deadline means the model accepted the request and never answered.

**What happens.** The step is left owed rather than recorded as failed, and the
run resumes from the last completed node. Re-running costs only the steps that
did not finish.

**Do not poll to check recovery.** A watcher once probed a model with a
20-per-day limit forty times, and the 503s still counted against the quota.

### A queued run never starts

**Check the worker.** If it is crash-looping (see `no pq wrapper`), the API will
keep accepting jobs that nothing runs.

**On free hosting**, the service sleeps when idle. A run in flight when it sleeps
stops mid-way — it is not lost, the reservation expires and the job is reclaimed
from its checkpoint, but **nothing wakes the service on the queue's behalf**. It
waits for a visitor.

---

## The browser client

### The page loads and every action fails

**Cause.** CORS. In a split deployment the API must name the site's origin in
`CORS_ORIGINS` — exactly, with the scheme, no trailing slash.

**Check.** A preflight should return 200 and echo your origin:

```bash
curl -i -X OPTIONS https://your-api/auth/login \
  -H "Origin: https://your-site" \
  -H "Access-Control-Request-Method: POST"
```

A 405 with no `access-control-allow-origin` means the variable is unset.

### Everything works except live progress

**Cause.** Almost always the content-security policy. `connect-src` must name the
API over **both** `https:` and `wss:` — allowing only the first leaves REST
working and the WebSocket blocked, which reads as a broken feature rather than a
policy.

Check the browser console for `Refused to connect`. This is invisible to `curl`,
because CSP is enforced by browsers and nothing else.

### Requests 404 in a split deployment

**Cause.** nginx proxies `/api` to the API **with the prefix stripped** — the
service serves `/auth/login`, not `/api/auth/login`. With no nginx in front,
adding the prefix 404s everything.

`VITE_API_ORIGIN` is therefore the whole base, not a host to prepend.

### The WebSocket closes immediately, or 403s

**Cause.** The ticket is single-use and lives **thirty seconds**. One fetched a
minute ago is expired. Get a ticket immediately before connecting.

Also check the path: it is `/research/{id}/events`, not `/events/{id}`.

### The first request after a quiet spell takes ~50 seconds

Working as intended on a free tier. The service spins down when idle and the
first request wakes it. Retry before concluding anything is broken.

---

## Development

### `make verify-deploy` fails on preconditions

It checks before it builds. `docker is not installed` is the usual one; the
others name the missing secret or certificate.

### Integration tests fail to connect

They need PostgreSQL and Redis running locally. `make test-int` after `make
db-up`.

### `deeptrace users create` succeeds but sign-in returns 422

**Cause.** The CLI once accepted addresses the API's `EmailStr` refuses —
`you@localhost` has no dot after the `@`. The account existed and could never
authenticate, and the 422 blamed the request rather than the account.

**Fixed**: the CLI now validates the same way. Mentioned because accounts created
before the fix are still unusable.

### A measurement disagrees with what you can see

Two traps, both hit repeatedly while working on this client:

**A computed style read immediately after a click is a frame of a CSS
transition**, not a result. Reload and wait, or read the class rather than the
colour.

**`clientHeight` excludes borders.** `offsetHeight - clientHeight == 1` on an
element with `border-b` is the border, not a reserved scrollbar.

A single measurement is not a fact until you know what else could produce that
number.
