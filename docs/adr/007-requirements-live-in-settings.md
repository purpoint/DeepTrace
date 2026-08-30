# ADR-007: Required credentials are enforced by the application, not by compose

**Status:** Accepted · **Date:** 2026-08-29 · **Supersedes:** the `${VAR:?}` guards added in M24

## Context

`docker-compose.yml` used `${JWT_SECRET:?message}` so the stack refused to start
without a signing key — a key with a default is a key every deployment shares.

M26 added secrets supplied as *files* under `/run/secrets`, for a deployment
overlay.

## Decision

The requirement moved into `Settings`: `APP_ENV=production` refuses to start
without `JWT_SECRET`, `DATABASE_URL`, `GOOGLE_API_KEY` and `TAVILY_API_KEY`, and
names every missing one in a single message.

## Consequences

**The two mechanisms could not coexist.** Compose interpolates each file *before*
merging an overlay, so a `:?` in the base file is demanded even from a deployment
that has deliberately replaced that variable with a mounted file. Discovering
this cost a failed deploy.

**The compose guard was also the weaker check.** It guards one way of starting the
application, tests only that a string is non-empty, and lives where no test can
see it. As a property of `Settings` it holds for compose, for Kubernetes, for a
systemd unit and for a shell, and it is a unit test.

**Consequence accepted:** a local instance still starts without credentials, and
the layer that needs one demands it at the point of use. That is deliberate —
`deeptrace check` should run on a fresh clone with no keys at all.
