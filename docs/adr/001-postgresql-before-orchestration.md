# ADR-001: Build PostgreSQL persistence before the orchestration layer

**Status:** Accepted · **Date:** 2026-08 · **Supersedes:** the milestone order in `milestones.md` (M7 before M14)

## Context

The original milestone order built the LangGraph workflow (M7) seven milestones
before PostgreSQL (M14).

LangGraph's value in this system is *checkpointed, resumable state*. A
checkpointer needs a durable store.

## Decision

Persistence first.

## Consequences

Building the graph in memory and adding Postgres later would have meant
rewriting every node's input/output contract and redoing the state layer — a
cost paid to preserve an ordering that had no other justification. Reordering
cost nothing, because nothing depended on the graph existing first.

The benefit showed up immediately at M15: a worker killed mid-run resumes from a
checkpoint that was already there, rather than from a feature added afterwards.

**What it did not prevent:** the checkpointer speaks psycopg3 while the
application speaks asyncpg, so the project carries two Postgres drivers. That is
a real cost of LangGraph's choice, not of this ordering, and it surfaced again
when a managed provider's connection string needed different SSL spellings for
each driver.
