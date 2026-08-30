# Interview answers

Twenty-nine questions, answered from this project rather than in general. Where a
number appears it was measured, and where something was cut or is still broken it
says so — an answer that only describes successes is not preparation, it is a
script.

---

# Architecture

### Why Python?

The libraries that matter here — Pydantic for structured contracts, LangGraph for
checkpointed orchestration, the provider SDKs — are Python-first, and the ecosystem
around evaluation and data handling is where the work actually is. Nothing in this
system is CPU-bound: it waits on network calls, so the runtime's speed is not the
constraint. Async matters more than throughput, and Python has it.

### Why FastAPI?

Async end to end, so a request that waits on Postgres does not hold a thread. Native
Pydantic, which matters more than it sounds: the same models validate the API
boundary and the contracts between agents, so there is one definition of a claim
rather than two that drift. And OpenAPI for free — the interactive schema at `/docs`
is generated from the models the routes validate against, so it cannot describe an
endpoint that no longer exists.

### Why LangGraph?

**For checkpointed, resumable state — not for orchestration in the abstract.** The
same nine steps are expressible as function calls. What is not, cheaply, is: a
worker killed halfway through resuming from where it stopped.

It forced a design change that turned out right. Nodes originally caught every
exception and turned it into state, which was correct while state lived in memory.
Once checkpointed it inverted — recording a 503 as a failure ended the run, so
nothing was pending, and resuming returned the same failure while the analysis
already paid for sat in the checkpoint. Nodes now separate *"this research cannot
proceed"* from *"the request could not be served"*.

Costs: a second Postgres driver (the checkpointer speaks psycopg3, the app asyncpg),
and an iteration ceiling I have to size by hand.

### Why PostgreSQL?

The checkpointer needs a durable store, and the data is relational: a claim points
at evidence, evidence points at a source, a source belongs to a task. That is a join,
not a document.

**I built it before the workflow engine**, out of the original milestone order.
Building the graph in memory and adding Postgres seven milestones later would have
meant rewriting every node's contract. Reordering cost nothing.

### Why Redis?

Two jobs. A queue between the API and the worker, because research takes minutes and
cannot happen inside a request. And the pub/sub plus history behind progress events.

I wrote the queue by hand, about 300 lines. **One guarantee was needed** — a job taken
by a worker that dies is taken again — and Celery brings a worker model, a
serialisation format, a scheduling story and a result backend as well. Four decisions
inherited to obtain one.

### Why workers?

Because a research run is two to fifteen minutes and an HTTP request is not. The API
accepts, validates, enqueues and returns **202 in 3.8ms**; the work
happens elsewhere.

It also makes the failure model honest. A worker can die and the job survives, because
the job is a row in Redis rather than a stack frame in a web process.

### Why WebSockets?

A user watching a spinner for five minutes assumes the system is broken.

**The interesting part is not the socket, it is the numbering.** Events carry sequence
numbers and a history is kept alongside the live stream, so a client that reconnects
asks for everything after the last one it saw. A stream that only pushes live events
loses whatever happened while the socket was down — which is invisible in development,
where sockets do not drop.

### Why pgvector?

**It is not installed, and that is the answer.** The roadmap gave it one job:
retrieving semantically related evidence across the whole run, so the fact checker
catches a contradiction that surfaced in a *different* task. It also gave a cut
criterion — if task-linked retrieval proved sufficient, pgvector goes.

Retrieval is lexical today. The cost is real and pinned by a test: a contradiction
phrased in different words is missed. I would rather ship a stated limitation than an
unjustified dependency, and the interface is a Protocol so swapping in vectors is one
implementation.

### Why multi-agent? / Why not one agent?

Seven specialists, one responsibility each, because **the pipeline's value is that
each stage can refuse the previous one's output.**

A single agent holding the whole task cannot check itself: the thing that wrote the
claim is the thing judging it, and it agrees. Separating them means the fact checker
sees a claim and evidence — including evidence the claim did *not* cite — with no
memory of why the claim seemed reasonable.

The reporter is the clearest case. It receives **claims only**: no sources, no search
results, no analyst reasoning. It cannot cite a page the fact checker rejected because
it is never shown one, which is stronger than instructing it not to.

### Why not just LLM + search?

That produces fluent text with citations that may or may not exist. It is the thing
this project was built to be an alternative to.

The difference is one mechanism: **every quotation is checked against the page it came
from, by string matching**, and anything absent is dropped along with the claim
resting on it. Measured on the runs that completed: citation correctness 0.99,
groundedness 1.00.

---

# AI Engineering

### What is an agent?

A model in a loop with tools and a stopping condition. The loop is what makes it an
agent rather than a completion; the stopping condition is what makes it finish.

In this system there are seven, and **none of them decides when to stop** — the
ceilings are arithmetic in code. An agent that decides its own budget is a bill.

### What is tool calling?

Letting a model choose a function and its arguments, then executing it and returning
the result. Here the tools are search, fetch, and nothing else that touches the
outside world.

The important part is what happens either side: the SSRF guard checks a URL before any
request is made, and everything returned is sanitized and fenced before a model reads
it. **A tool result is untrusted input, not a fact.**

### What is RAG?

Retrieving relevant text and putting it in the prompt so the model answers from it
rather than from memory.

This is RAG with the verification step most implementations skip. Retrieval alone
gives the model material; it does not stop the model quoting something the material
never said. So every extracted passage is re-checked against the retrieved text, and
the claim built on an unverifiable passage is dropped.

### What are embeddings?

Vectors positioning text by meaning, so similar text sits nearby and similarity
becomes arithmetic.

**This project does not use them**, and the honest reason is in "Why pgvector" above:
the one job that justified them was cross-task contradiction retrieval, and lexical
retrieval has been adequate so far. The model is configured (`gemini-embedding-001`)
and unused.

### What is hallucination?

A model producing fluent, confident, well-formed output that is not true. In this
domain it has a specific and dangerous shape: **a quotation that reads exactly like
something the page would say, and is not on it.** By eye, nothing distinguishes it
from a real one.

### How is grounding achieved?

Four layers, and each drops something rather than flagging it:

1. **Quote verification** — the passage is found in the retrieved text, or rejected.
2. **Claim grounding** — a claim with no evidence link is not publishable.
3. **Fact checking** — each claim is re-checked against evidence it did not cite.
4. **Assembly** — the report is built from surviving claims; citation numbers are
   assigned by this codebase, not by the model.

### How are citations verified?

Deterministic string matching, with normalisation for whitespace and invisible
characters. **Not a model call** — asking a model to validate a model's quote
reintroduces the failure it is meant to catch.

Three statuses, because a paraphrase and a quotation are different kinds of support:
`verbatim`, `normalised`, `paraphrase`.

**Where this went wrong, and it is the best answer I have.** Verifying the passage is
not verifying the *reference* to it. The citation marker pattern matched a single
bracketed number, so `[1, 2, 3]` — the form a model actually writes — was never
parsed, never checked, never removed. An invented number inside a group reached the
reader **with the report still declaring itself fully cited**. I found it by reading a
report the deployed system served, not by testing. Groups are now filtered number by
number.

### How are conflicts handled?

Reported, not resolved. Where sources disagree, the disagreement *is* the finding: the
report has a "Where sources disagree" section giving both positions and who holds
them, and a claim marked conflicting is presented as a disagreement rather than
averaged into a single confident sentence.

Measured, and not flattering: **1 of 7 contested questions produced a reported
disagreement.** That is in `EVALUATION.md` rather than omitted.

### How are loops prevented?

Numeric ceilings in code, plus two convergence checks. Depth budgets on tasks,
sources, verification loops and tokens; a round limit inside the research agent; and a
graph iteration limit of 60 — sized from what a legitimate run costs, since fan-out
means one step per task plus one per wave, so the worst case is `2 × max_tasks + 4`.

**None are prompt instructions**, so no model can reason past them.

### How is quality evaluated?

24 questions across five research types, seven deterministic metrics, and **no
expected answers** — because writing the answers would measure agreement with me
rather than quality.

Nothing asks a model to judge a model. A judge that shares the generator's blind spots
agrees with it, and the agreement scores well.

**The baseline covers 3 of 24 questions.** The free tier allows 20 model requests a
day and a run spends about seven, so the suite cannot complete in one sitting. The
report states that denominator beside every figure and refuses to present the mean of
three as the benchmark score.

---

# Production

### How would you scale to 10,000 jobs?

The queue and workers already handle it in shape: workers are stateless, the queue is
at-least-once, and jobs carry their research id so any worker can pick any job up.
`docker compose up --scale worker=N` today.

**The bottleneck is not the architecture, it is the provider.** A run makes about
seven model calls against an account-wide rate limit. Ten thousand jobs is a
provider-quota problem and a cost problem long before it is a concurrency problem —
which I know because a shared token bucket was needed at *one* worker.

What would actually need building: per-tenant quotas, backpressure so the queue
refuses rather than accumulates, and a cheaper path for repeated questions.

### How would you reduce LLM cost?

Measured first, because guessing here is how you optimise the wrong stage. The trace
shows cost by agent: **three strong-tier calls per run — planner, analyst, reporter —
dominate.**

In order of effect: route more aggressively (the analyst may not need the strong tier
for simple questions); cache search and extraction, which are already keyed on
deterministic inputs; and reduce evidence sent to the analyst, since input tokens
scale with passages collected.

Not caching completions by default — non-determinism is sometimes desirable, and stale
synthesis is worse than a repeated call.

### What if search is down?

The task records `insufficient` and the run continues with what it has. A search
failure is not a run failure: other tasks may have found enough, and the report says
what could not be established rather than pretending.

`SearchProvider` is a Protocol, so a second provider is one module. Not built —
Tavily has not failed in a way that justified it.

### What if a worker crashes?

Its heartbeat stops, the reservation expires, another worker reclaims the job — and
because the job carries the research id, the second attempt **resumes from the
checkpoint** rather than repeating paid work.

**I proved it with SIGKILL forty seconds into a run.** The replacement resumed at
synthesis, and the database shows zero searches and zero extraction calls in that
attempt against 8 sources and 24 passages in the finished run.

This paid for itself unexpectedly on free hosting: the API sleeps when idle, so a run
in flight stops mid-way and is reclaimed when a visitor next wakes the service.

### How do retries work?

Three layers, deliberately separate:

- **Inside the LLM client** — bounded jittered backoff, driven by a `retryable` flag
  on typed errors. A structured-output failure is retryable but not transient, so the
  repair loop feeds back the raw output and the validation error rather than repeating
  an identical request.
- **The graph** — a step that could not be served is left owed; resume continues from
  the last completed node.
- **The queue** — at-least-once, with reclaim.

The distinction that matters: `retryable` asks *"try again now?"*; `transient` asks
*"could this be served at all?"*.

### How do you prevent SSRF?

Every URL is checked **before any request is made**: scheme allowlisted, hostname
**resolved**, and every resolved address checked against loopback, link-local, private
and reserved ranges — including `169.254.169.254`, the cloud metadata endpoint. A
rejected URL is not retried on another address.

Resolving is the part that matters: `http://evil.test` resolving to `127.0.0.1` is the
attack, and checking the string would miss it.

**Not closed: DNS rebinding.** The guard resolves and checks, then the HTTP client
resolves again. Closing it means pinning the checked address into the connection. It
is documented rather than claimed.

### How do you prevent prompt injection?

Layered, and I can tell you which layers are real.

Sanitization removes what a reader would never see. The fence wraps untrusted text in
a delimiter with a preamble. Quote verification rejects a fabricated citation even
under full compliance. The SSRF guard refuses the fetch an exfiltration needs. Claim
grounding drops an assertion with nothing behind it.

**Twelve attacks in a corpus, each paired with the layer expected to stop it. Seven
are deterministic. Five are not**, and `model_dependent()` names them — a corpus whose
every case rests on "the model behaved" measures the model's mood.

**The corpus found a real hole in the defence it was written to test.** The fence used
a *fixed* delimiter, so a page could write the closing token itself and step outside
the preamble governing it. There was already a test asserting injections stay inside
the fence, and it passed throughout — its attack was ordinary text that never tried to
write a delimiter. **A containment test is worth what its attack attempts.** The
delimiter now carries a per-call nonce.

### How do you monitor quality?

Three things, and only the first is automatic.

**Per run**: the trace records every model and tool call with prompt version, tokens,
latency and cost; the report exposes unresolved citation markers and unsupported claim
ids rather than hiding them; `fully_cited` is a field a reader can see.

**Across runs**: the benchmark, with deterministic metrics and stated denominators.

**What is missing**: nothing watches the metrics *over time*. There is one baseline,
covering 3 of 24 questions, and two citation fixes since that have not been measured
against it. Quality monitoring today means running the benchmark and reading it.

---

### What did you ship without ever looking at?

One screen, and it was the one that most needed looking at.

The Progress view shows a run happening — the seven stages, and every event as it
arrives. Seeing it therefore requires a run **in flight**, and a run costs about seven
of the twenty model requests the free tier allows in a day, competing directly with the
benchmark. So it was written, tested at the hook below it, shipped, deployed twice, and
never opened.

Reading it was enough. `useProgress` took an `enabled` flag and the workspace passed
`running`, so for a **finished** run the hook did not connect at all and the screen had
no events to render. That draws as seven unticked stages under the heading
"Researching", subtitled **"Live"**, above a spinner reading *"Waiting for the worker to
pick this up"* — over a run that completed days earlier. And the commit that made the
workspace tabs addressable had quietly turned it into a URL someone can send to someone
else.

The server was never the problem. It replays a run's history and closes as soon as it
sends a terminal event; the client was the only half that refused to ask. So `live` no
longer means "connect or do not" — the hook always connects, because replay is the
point — and now means "more events are possible", governing only what a quiet or closed
socket signifies: a pause to recover from, or the end of the recording.

Removing that gate needed two guards the old behaviour had been providing by accident. A
close without a terminal event is what a run whose events have aged out of the capped
history produces, and retrying it reconnects forever on a page nobody expects to be
working. And a quiet socket would otherwise be held for the server's 300-second idle
timeout, spinning. The second is fixed by noticing that a heartbeat — which the server
sends when a poll finds nothing — *means* end-of-recording once nothing more can come.

Two things I would want to be asked about here. **The cheap half of "found by looking"
is reading**, and it was available at any point in the last month for nothing. And the
reason it went unread is a real engineering constraint rather than laziness: a quota
that makes observing your own system compete with measuring it. That is worth saying out
loud, because it will be true of anything metered.

---

# What I would claim, and what I would not

The resume section of the milestone list says *"only after implementation and
measurement"*. Applying that honestly:

**Can be claimed, because it was measured:**

- Rate limiting: 64 provider errors and 44% of collected sources wasted, before. After
  a shared token bucket: zero errors, every source mined, **48% more evidence, at 3.7×
  latency.**
- Parallel research: the stage it affects halved, **30.0s of tool work to 15.2s**, with
  three tasks genuinely overlapping.
- Crash recovery: verified by SIGKILL, with the database showing zero repeated work.
- Sanitizer: lossless on **20,835 words** of real documentation.
- API accepts and enqueues, returning 202 in **3.8ms**.
- The free tier's sleep penalty: a cold `/health` on the deployed API answered 200 in
  **52.8s** on 2026-08-30. Documented as "~50 seconds" before that; now measured.
- On the runs that completed: citation correctness **0.99**, groundedness **1.00**,
  verbatim rate **0.94**.

**Cannot be claimed:**

- Any benchmark score as a headline figure. **It covers 3 of 24 questions**, and a
  mean over three is not a benchmark result.
- Total cost. 22 of 24 runs have no recorded price, so there is no total — and
  reporting `$0.00` would make an unmeasured run look free.
- That parallelism made runs faster. The stage halved; **total wall clock did not
  move**, because at the provider rate I am limited to, a run making thirty model
  calls has a floor near three minutes however the work is arranged. I kept it because
  it makes an interrupted wave resume owing only the tasks that did not finish.
- Latency figures as targets met. The roadmap's per-depth targets were design goals; the
  measured mean is 48.2s across three quick-depth runs, which is not the same claim.

**The most useful thing I can say about this project** is that its failures are
written down. `TROUBLESHOOTING.md` contains only errors that actually happened.
`SECURITY.md` names the five attacks nothing deterministic stops. `EVALUATION.md`
refuses to average three runs into a score. That was harder than making the numbers
look good, and it is the part I would want to be asked about.
