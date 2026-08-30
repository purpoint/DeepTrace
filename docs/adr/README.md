# Architecture Decision Records

One file per decision that would be expensive to reverse or surprising to
inherit. Each states the context, the decision, and the consequences — including
the ones that turned out badly.

A record is never rewritten when a decision changes. A superseding record is
added and the original marked, because **a decision log that quietly matches
what happened teaches nothing about which decisions moved.**

| # | Decision | Status |
|---|---|---|
| [001](001-postgresql-before-orchestration.md) | PostgreSQL before orchestration | Accepted |
| [002](002-langgraph-for-workflow.md) | LangGraph for the workflow | Accepted |
| [003](003-redis-hand-written-queue.md) | A hand-written queue on Redis | Accepted |
| [004](004-websockets-for-progress.md) | WebSockets for progress, with replay | Accepted |
| [005](005-gemini-over-openai.md) | Gemini rather than OpenAI | Accepted, supersedes the roadmap |
| [006](006-deterministic-quote-verification.md) | Quote verification without a model | Accepted |
| [007](007-requirements-live-in-settings.md) | Required credentials belong to the application | Accepted, supersedes the compose guards |
