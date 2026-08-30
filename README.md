# DeepTrace

**Autonomous AI Research & Evidence Synthesis Platform**

DeepTrace decomposes a complex research question into subtasks, researches them across
the web, extracts source-backed evidence, verifies claims against that evidence, and
produces a citation-grounded report with a fully inspectable research trace.

> The core promise: DeepTrace doesn't just give you an answer. It shows you how the
> answer was researched, what evidence supports it, and where uncertainty remains.

---

## Why this exists

A single LLM call can produce a convincing research answer. It can also produce outdated
information, fabricated citations, and confident conclusions with nothing behind them —
and it looks identical either way.

DeepTrace treats research as an explicit, inspectable workflow rather than a single
generation step:

```text
Question → Analysis → Plan → Parallel Research → Evidence
        → Claims → Verification → Report + Citations + Trace
```

Every claim in a finished report can be traced back to the passage that supports it:

```text
Report  →  Claim  →  Evidence  →  Source  →  URL
```

---

## Design principles

1. **Evidence before conclusions.** Claims without supporting evidence do not reach the report.
2. **The LLM is one component, not the application.** Reasoning, orchestration, tool execution, persistence, and verification are separate, independently testable layers.
3. **No single agent, no single provider.** Six specialized agents, each with one responsibility; a provider-agnostic model layer so no agent knows which LLM vendor it is running on.
4. **Deterministic tools around probabilistic models.** Tools do not reason. Agents do not fetch.
5. **Retrieved web content is untrusted data**, never instructions.
6. **Measured, not demonstrated.** Quality is reported from an evaluation benchmark, not from a good demo.

---

## Architecture

```text
   React Workspace
         │ HTTPS / WebSocket
         ▼
      FastAPI  ──────────────┐
         │                   │
         ▼                   ▼
   Redis + Workers      PostgreSQL
         │              (+ pgvector)
         ▼
   LangGraph Workflow
         │
         ├── Planner Agent
         ├── Research Agent ──→ Tool Layer ──→ Web
         ├── Evidence Agent
         ├── Analyst Agent
         ├── Fact Checker ──→ (insufficient evidence → research again)
         └── Report Writer
```

Architecture decision records and full subsystem documentation are added in Phase K.

---

## Technology

| Layer | Choice |
|---|---|
| Backend | Python 3.13, FastAPI, Pydantic, SQLAlchemy, Alembic |
| AI | Provider-agnostic LLM layer (OpenAI first), LangGraph orchestration |
| Search | Tavily (behind a provider interface) |
| Data | PostgreSQL, pgvector, Redis |
| Frontend | React, Vite, TypeScript, Tailwind, TanStack Query |
| Quality | pytest, ruff, evaluation benchmark |
| Deploy | Docker Compose, GitHub Actions |

## Project status

Built sequentially. This table reflects what actually works, not what is planned.

| Phase | Scope | Status |
|---|---|---|
| A | Foundation, LLM layer, cost + prompt versioning | ✅ |
| B | Query analyzer, research planner | ✅ |
| C | Tools, researcher agent, evidence system | ✅ |
| D | PostgreSQL persistence | ✅ |
| E | LangGraph workflow, parallel research | ✅ |
| F | Analyst, claims, fact checker | ✅ |
| G | Report generation, citations | ✅ |
| H | Redis workers, FastAPI, WebSockets | ✅ |
| I | React workspace, authentication | ✅ |
| J | Evaluation, observability, optimization | ⬜ |
| K | Docker, CI/CD, deployment, docs | ⬜ |

Phase J has numbers for 3 of its 24 benchmark questions -- see
[EVALUATION.md](EVALUATION.md), which states that denominator beside every
figure. The free tier allows 20 model requests a day and a question spends
three, so the rest is a matter of days rather than of code. No benchmark number
is claimed before it is recorded.

Running it in containers, with TLS and file-based secrets, is in
[DEPLOYMENT.md](DEPLOYMENT.md) — which also covers the deployed instance on
Render, and what a free tier costs the design.

---

## Local setup

Requires Python 3.13+, PostgreSQL 15+, and Redis 7+.

```bash
git clone https://github.com/purpoint/DeepTrace.git
cd DeepTrace
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
```

Or simply:

```bash
make setup
```

Add your API keys to `.env`, then verify:

```bash
make check
```

`.env` also needs a `JWT_SECRET`. The API refuses to start with one shorter than
32 characters: HS256 keys are brute-forceable offline, where there is no server
in the loop to rate limit the attempt.

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

`deeptrace status` prints the resolved configuration and depth budgets;
`deeptrace check` reports foundation health and what is still pending.

To run research:

```bash
deeptrace research "Compare Kafka and RabbitMQ for high-scale microservices" \
    --depth quick --checkpoint --save
```

`--save` writes the run and its evidence to PostgreSQL. `--checkpoint` writes the
workflow state after each step, so a run stopped by an outage can be continued
rather than paid for again:

```bash
deeptrace resume res_f460fa9e6f5740f0
```

### The service

Three processes: the API, at least one worker, and the browser client.

```bash
make db-up      # apply migrations
make api        # http://127.0.0.1:8000  (docs at /docs)
make worker     # consumes research jobs
make web        # http://localhost:5173
```

Every research endpoint requires an account, and answers only for the research
belonging to it — a run someone else owns is reported as though it does not
exist. Register through the sign-in screen, or create the first account from the
shell:

```bash
deeptrace users create you@example.com
```

A run started with `deeptrace research` has no owner and is therefore not
visible through the API. Use `deeptrace submit --as you@example.com` to queue one
that belongs to an account.

---

## Repository layout

```text
apps/            FastAPI service, background worker, React frontend
core/            Agents, workflow graph, tools, prompts, evaluation
infrastructure/  Database, auth, cache, and queue adapters
tests/           Unit, integration, workflow, and evaluation tests
scripts/         Development and operational scripts
```

---

## License

MIT — see [LICENSE](LICENSE).
