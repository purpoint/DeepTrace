# ADR-005: Google Gemini rather than OpenAI

**Status:** Accepted · **Date:** 2026-08 · **Supersedes:** the locked decision in the original roadmap

## Context

The roadmap's first locked decision named OpenAI, for native JSON-schema
structured outputs, mature tool calling and two clear price tiers.

Every agent in this system returns structured data. A provider that can only be
*asked* for JSON means a repair loop on nearly every call.

## Decision

Gemini, during Phase A.

## Consequences

Gemini has native JSON-schema structured output too, on a free tier — which
matters for a project with no budget, where the binding constraint turned out to
be quota rather than money.

**The change cost one module and one config value, and no agent changed.** That
is the provider abstraction earning its keep as evidence rather than as theory:
agents depend on a four-method Protocol, and an adapter is forbidden from
retrying, logging, pricing or validating, so those live above the interface and
were written once.

**What it cost.** Gemini rejects standard JSON Schema, so `$ref` needed inlining
and unsupported keywords dropping — inside the adapter, which is the point of
having one. And the free tier's ceiling is low: **20 model requests per day**,
against roughly seven per research run. That single number now shapes the
evaluation baseline, the deployment's capacity, and how this system can be
demonstrated at all.

**And it is not stable.** `gemini-3.7-flash` stopped answering on 2026-08-25 —
accepting requests and never replying, a trivial prompt returning 504 after a
300-second deadline. The strong tier is pointed at `gemini-3.5-flash`, declared
rather than quietly switched: `EVALUATION.md` stamps the model on every figure so
the numbers cannot be read without reading which model produced them.
