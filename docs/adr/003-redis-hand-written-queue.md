# ADR-003: A hand-written job queue on Redis

**Status:** Accepted · **Date:** 2026-08

## Context

Research runs for minutes. It cannot happen inside an HTTP request, so it needs
a queue and a worker.

Celery, RQ, Dramatiq and arq all solve this.

## Decision

A hand-written queue on Redis, roughly 300 lines.

## Consequences

**One guarantee was needed**: a job taken by a worker that dies is taken again.
A library brings a worker model, a serialisation format, a scheduling story and
a result backend as well — four decisions inherited to obtain one.

The implementation is a reservation with a heartbeat and an expiry. A stalled
job is reclaimed; because the job carries the research id, the second attempt
resumes from the checkpoint rather than repeating paid work. Verified by SIGKILL
at forty seconds: the replacement resumed at synthesis, with zero searches and
zero extraction calls in that attempt.

**Consequences accepted.** At-least-once, not exactly-once, so the pipeline must
tolerate a repeated attempt — which the checkpointer makes cheap. No scheduled
or delayed jobs. No fan-out primitives; the graph does its own fan-out. If any
of those become necessary, this decision should be revisited rather than
extended.

**A consequence not anticipated:** on free hosting the API sleeps when idle, so a
run in flight stops mid-way and waits for a visitor to wake the service before it
is reclaimed. The queue makes that survivable rather than data loss.
