# Architecture

DeepTrace answers hard questions and shows its work. This describes how, and —
more usefully — why each piece is shaped the way it is.

The organising idea is one sentence:

> Every claim in a report traces to a passage, and every passage was checked
> against the page it came from.

Almost every decision below follows from defending that sentence against the
ways a language model breaks it.

---

## 1. The shape of a run

```
START → analyze → plan → dispatch → evidence → analysis → claims → verify → report → END
                            ↑  │                                      │
                            │  └→ research_task ×N (one per task)     │
                            └─────────────────────────────────────────┘
                                    additional research, budgeted
```

Nine nodes in a LangGraph state machine. Research **fans out**: `dispatch` sends
one node per task in the current wave, tasks run concurrently under a semaphore,
and control returns for the next wave. Verification can extend the plan and send
the run back to `dispatch`, bounded by the depth budget's loop allowance.

| Node | Does | Tier |
|---|---|---|
| `analyze` | Turns a question into a specification: type, scope, success criteria, ambiguities | cheap |
| `plan` | Decomposes it into atomic, independently researchable tasks | strong |
| `dispatch` | Selects the next wave; the fan-out point | — |
| `research_task` | Searches, fetches, scores sources for one task | cheap |
| `evidence` | Extracts passages and **verifies each against the retrieved text** | cheap |
| `analysis` | Draws findings, trade-offs and contradictions from verified evidence | strong |
| `claims` | Turns the analysis into claims linked to the evidence behind them | — |
| `verify` | Checks each claim against evidence it did not cite | cheap |
| `report` | Writes the document from claims that survived | strong |

`claims` makes no model call at all. Asking a model to restate its own
conclusions as claims would add cost, latency, and a second chance to invent
something. It exists as a stage because verification needs somewhere to stand
that is not inside the analyst, and because it is a checkpoint boundary — a
crash between analysis and claims would otherwise re-run a paid strong-tier
call to redo work that costs nothing.

---

## 2. Layers, and the rule that holds them apart

```
core/            the research engine — never imports apps/ or infrastructure/
  agents/        seven specialists, one responsibility each
  evaluation/    benchmark dataset, deterministic metrics, harness, injection corpus
  graph/         LangGraph state, nodes, routing, checkpoint serialisation
  llm/           vendor-neutral provider interface + Gemini adapter
  models/        Pydantic contracts between agents
  observability/ run recording, progress emission, tracing
  prompts/       versioned prompts + untrusted-content fencing
  redaction.py   one definition of what a secret looks like
  retrieval.py   evidence retrieval interface (lexical today, vector later)
  tools/         deterministic external actions (search, fetch, SSRF guard, sanitizer)
  pipeline.py    composition root: run_research / resume_research
  cli.py         status · check · pricing · evaluate · research · resume ·
                 submit · work · jobs · serve · users

infrastructure/  adapters — may import core, never the reverse
  auth/          Argon2 passwords, JWT minting and verification, session records
  db/            SQLAlchemy models, migrations, repositories, recorder, checkpointer
  queue/         job queue, job model, progress event stream
  rate_limit.py  sliding-window limiter (inbound)

apps/            deployable entry points
  api/           FastAPI: routes, schemas, errors, dependencies
  worker/        the job runner
  web/           React browser client
```

**`core` never imports `apps` or `infrastructure`.** That is why the research
engine runs from a script, a worker, a test, or an HTTP request without changing
a line. Everything external arrives through a Protocol: `LLMProvider`,
`SearchProvider`, `RunRecorder`, `ProgressEmitter`, `EvidenceRetriever`.

The rule earns its keep constantly. The benchmark harness injects a `RunResearch`
callable and can be tested against constructed runs without spending quota. The
recorder was a JSONL file writer before it was a Postgres table, and no call site
changed.

---

## 3. The mechanisms that define the system

### 3.1 Quote verification — the anti-fabrication core

Ask a model for a supporting quote and it will sometimes produce a sentence that
reads *exactly* like something the page would say, and isn't on it. By eye,
nothing distinguishes the two.

So every extracted passage is checked against the text actually retrieved, by
**deterministic string matching**. Absent passages are rejected, along with the
claim attached to them. It is not a model call, because asking a model to
validate a model's quote reintroduces the failure it is meant to catch.

Three statuses, and the distinction matters: `verbatim` (exact), `normalised`
(matched after whitespace and invisible-character folding), `paraphrase` (found
by overlap, shown as weaker support). Flattening them would present the weakest
as the strongest.

### 3.2 The citation marker, and what it nearly let through

Citation numbers belong to this codebase, not to the model. The reporter writes
`[3]`; the mapping from 3 to a passage in a page is assembled here, so a number
the model invents resolves to nothing and is removed before anyone reads it.

That held for `[3]` and **failed for `[1, 2, 3]`** — the form a model actually
writes when several passages support one sentence. The marker pattern matched a
single number, so grouped markers were never parsed, never checked, and never
removed: an invented number inside a group reached the reader with the report
still declaring itself fully cited. Found by reading a report the deployed system
served. Groups are now filtered number by number, keeping the valid half rather
than discarding a real citation because a wrong one was appended to it.

### 3.3 Bounded execution

Every ceiling is arithmetic, not a prompt instruction, so no model can reason
past it.

| depth | tasks | sources | verify loops | tokens |
|---|---|---|---|---|
| quick | 3 | 8 | 0 | 40,000 |
| standard | 6 | 20 | 1 | 150,000 |
| deep | 12 | 50 | 3 | 500,000 |

`max_graph_iterations` defaults to 60, sized from what a legitimate run costs:
fan-out means one step per task plus one per wave, so the worst case is
`2 × max_tasks + 4` — 28 at the deep budget.

### 3.4 Untrusted content

Retrieved web pages are data, never instructions. Two defences, and the first is
the one that was wrong:

**The fence.** Retrieved text is wrapped in a delimiter the model is told not to
treat as instruction. The delimiter used to be *fixed*, so a page could write the
closing token itself and step outside the preamble that governs the fence. It now
carries a per-call nonce, and delimiter-shaped text is stripped from the body.
Found by the prompt-injection corpus written to test it — an existing containment
test had passed throughout, because its attack never tried to write a delimiter.
A containment test is worth what its attack attempts.

**Sanitization at ingestion**, in the records themselves, so every search provider
and every fetched page inherits it. Removes executable blocks, hidden text and
directional controls, and deliberately nothing else: verified lossless on 20,835
words of real documentation, because a sanitizer that eats `List<String>` corrupts
the evidence this project exists to protect.

### 3.5 At-least-once delivery

The queue is hand-written on Redis because only one guarantee was needed, and a
library brings a worker model, a serialisation format and a scheduling story too.

A job taken by a worker that dies is taken again. Its heartbeat stops, the
reservation expires, another worker reclaims it — and because the job carries the
research id, the second attempt **resumes from the checkpoint** rather than
repeating paid work. Proved by killing a worker with SIGKILL forty seconds in: the
replacement resumed at synthesis, and the database shows zero searches and zero
extraction calls in that attempt against 8 sources and 24 passages in the finished
run.

That machinery paid for itself outside a test for the first time on free hosting,
where the API sleeps when idle: a run in flight when the service sleeps stops
mid-way, and is reclaimed and resumed when a visitor next wakes it.

### 3.6 Shared rate limiting

Built from a measurement, not a guess. Under load: 64 rate-limit errors and 44%
of collected sources wasted. Per-agent concurrency did not help, because the
provider's limit applies to the account rather than to a caller. A **shared token
bucket** that a 429 pauses globally fixed it: zero errors after, every source
mined, 48% more evidence, at 3.7× latency.

### 3.7 Progress, losslessly

Events are numbered and kept in a history alongside the live stream, and a client
reconnecting asks for everything after the last sequence it saw. A stream that
only pushes live events loses whatever happened while the socket was down —
invisible in development, where sockets do not drop.

---

## 4. Locked decisions

| Decision | Choice | Reason |
|---|---|---|
| LLM provider | Google Gemini | Free tier with *native JSON-schema* structured output. A provider that can only be *asked* for JSON means a repair loop on nearly every call. |
| Models | cheap / strong tiers, configured | Agents request a tier, never a model name |
| Search | Tavily | Returns extracted page content, not just links |
| Database | PostgreSQL | Checkpointing needs a durable store |
| Queue | Redis, hand-written | One guarantee needed |
| Orchestration | LangGraph + Postgres checkpointing | Resumable state is the reason to use it at all |
| API | FastAPI | Async end to end, native Pydantic, OpenAPI for free |
| Frontend | React + Vite + TS + Tailwind + TanStack Query | — |

Longer reasoning, and the decisions that changed, are in [`docs/adr/`](docs/adr/).

---

## 5. What this architecture does not do

Stated because an architecture document that lists only strengths is marketing.

- **Retrieval is lexical.** pgvector is not installed, so cross-task retrieval
  misses a contradiction phrased in different words. Pinned by a test.
- **The rate limiter keys on the socket's peer address.** Correct directly, and
  behind a proxy it needs `--forwarded-allow-ips` naming the trusted hop —
  deployment configuration, supplied in `render.yaml` and the compose overlay.
- **Access tokens cannot be revoked.** Fifteen minutes is the exposure window and
  the price of verifying without a lookup.
- **Two Postgres drivers**: asyncpg for the application, psycopg3 for LangGraph's
  checkpointer.
- **The evaluation baseline covers 3 of 24 questions**, capped by a free-tier
  quota of 20 model requests a day. See [EVALUATION.md](EVALUATION.md), which
  refuses to present the mean of three as the benchmark score.

---

## 6. Reading further

| Document | Covers |
|---|---|
| [API.md](API.md) | Every endpoint, the error envelope, authentication |
| [SECURITY.md](SECURITY.md) | Threat model, controls, and what is still open |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Containers, TLS, secrets, the hosted deployment |
| [EVALUATION.md](EVALUATION.md) | Measured numbers, and their denominators |
| [TROUBLESHOOTING.md](TROUBLESHOOTING.md) | Failures seen for real, and what each meant |
| [docs/adr/](docs/adr/) | Decisions, with the reasoning and the ones that changed |
