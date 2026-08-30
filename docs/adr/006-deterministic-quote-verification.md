# ADR-006: Verify quotations by string matching, never by model

**Status:** Accepted · **Date:** 2026-08

## Context

Ask a model for a supporting quote and it will sometimes produce a sentence that
reads exactly like something the page would say, and is not on it. By eye,
nothing distinguishes the two.

This is the failure the entire project exists to prevent.

## Decision

Every extracted passage is checked against the text actually retrieved, by
deterministic string matching. Absent passages are rejected, along with the claim
attached to them.

## Consequences

**Not a model call.** Asking a model to validate a model's quote reintroduces the
failure it is meant to catch: a judge that shares the generator's blind spots
agrees with it, and the agreement scores well. The same reasoning keeps every
evaluation metric deterministic.

Three statuses rather than a boolean — `verbatim`, `normalised`, `paraphrase` —
because a paraphrase and a quotation are different kinds of support and
flattening them presents the weaker as the stronger.

**It found a bug in its own favour.** Invisible characters in retrieved text were
causing genuine evidence to verify as `not_found` and be discarded — a real cost,
hidden behind a check nobody suspected because it was doing its job loudly.
Normalisation folds them now.

**Where it did not reach.** The check covers the passage. The *citation marker in
the prose* is a separate mechanism, and it matched only a single bracketed number
— so `[1, 2, 3]`, the form a model actually writes, was never parsed and an
invented number inside a group survived. Verifying the passage does not verify
the reference to it, and it took a deployed report to make that obvious.
