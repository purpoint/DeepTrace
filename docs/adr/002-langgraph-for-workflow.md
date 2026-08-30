# ADR-002: LangGraph for the research workflow

**Status:** Accepted · **Date:** 2026-08

## Context

The pipeline has a fan-out stage, a conditional loop back into research, and
must survive a worker being killed halfway through.

Written by hand this is a state machine, a serialisation format for its state,
and a resume path — three things to keep correct.

## Decision

LangGraph, with the Postgres checkpointer.

## Consequences

**The reason to use it is resumable state.** Not orchestration in the abstract:
a graph that cannot be resumed would not have earned the dependency, because the
same nine steps are expressible as function calls.

It forced a design change that turned out to be right. Nodes originally caught
every exception and turned it into state, which was correct while state lived in
memory. Once state was checkpointed it inverted: recording a 503 as a failure
ended the run, so nothing was pending, and resuming returned the same failure
while the analysis already paid for sat in the checkpoint. Nodes now separate
*"this research cannot proceed"* from *"the request could not be served"* — a
distinction the checkpointer made necessary and worth having anyway.

**Costs.** A second Postgres driver (see ADR-001). A serialisation layer for
custom state. And an iteration ceiling that has to be sized by hand — 60,
derived from `2 × max_tasks + 4`, because a graph with a loop in it can spin.
