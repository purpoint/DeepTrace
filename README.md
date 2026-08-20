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

Full detail in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) *(added in Phase K)* and the
build plan in [`docs/ROADMAP.md`](docs/ROADMAP.md).

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

Each choice is justified in an Architecture Decision Record under `docs/adr/`.

---

## Project status

Built sequentially. This table reflects what actually works, not what is planned.

| Phase | Scope | Status |
|---|---|---|
| A | Foundation, LLM layer, cost + prompt versioning | 🟡 In progress |
| B | Query analyzer, research planner | ⬜ |
| C | Tools, researcher agent, evidence system | ⬜ |
| D | PostgreSQL persistence | ⬜ |
| E | LangGraph workflow, parallel research | ⬜ |
| F | Analyst, claims, fact checker | ⬜ |
| G | Report generation, citations | ⬜ |
| H | Redis workers, FastAPI, WebSockets | ⬜ |
| I | React workspace, authentication | ⬜ |
| J | Evaluation, observability, optimization | ⬜ |
| K | Docker, CI/CD, deployment, docs | ⬜ |

Performance and cost figures will be published here once measured in Phase J. No
benchmark numbers are claimed before they are recorded.

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

Add your API keys to `.env`, then verify the install:

```bash
pytest
```

---

## Repository layout

```text
apps/            FastAPI service, background worker, React frontend
core/            Agents, workflow graph, tools, prompts, evaluation
infrastructure/  Database, cache, and queue adapters
tests/           Unit, integration, workflow, and evaluation tests
docs/            Roadmap, architecture, ADRs, learning notes
scripts/         Development and operational scripts
```

---

## License

MIT — see [LICENSE](LICENSE).
