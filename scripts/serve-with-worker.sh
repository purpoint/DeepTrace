#!/usr/bin/env sh
#
# The API and a worker, in one container, because a free hosting tier has one
# service type and it is a web service.
#
# **This is a compromise, not the design.** docker-compose.yml runs them as two
# services for reasons that still hold: a worker is scaled independently of the
# API, a research run outlives any request, and a worker that wedges should be
# replaceable without dropping traffic. None of that is true here.
#
# What it costs, stated plainly so nobody has to discover it:
#
#   * Scaling the API scales the workers with it, and vice versa.
#   * A free web service sleeps when no HTTP request has arrived for a while.
#     A run in flight when that happens stops in the middle. It is not lost --
#     the queue is at-least-once, the reservation expires, and the job is
#     reclaimed and resumed from its checkpoint when the service wakes -- but
#     nothing wakes the service on the queue's behalf, so it waits for a
#     visitor.
#   * If either process dies, this script exits so the platform restarts the
#     container. A worker that has quietly died inside a healthy-looking web
#     service is the failure this whole file risks, and the one thing it must
#     not do is hide it.
#
# Two services is one paid plan away. Use docker-compose.yml when that day
# comes; nothing in the application changes.

set -eu

# Migrations here, which docker-compose.yml deliberately does not do.
#
# There they are a one-shot job, because running them from the API means two
# replicas racing to migrate the same database on every deploy and Alembic is
# not safe under that. A free tier runs exactly one instance, so the race this
# guards against cannot happen -- and a free tier has no job type to put them
# in. The reasoning is unchanged; the circumstances are.
#
# It runs before anything serves. A container that starts and answers requests
# against a schema that has not been applied is worse than one that fails here.
echo "applying migrations" >&2
alembic upgrade head

python -m core.cli work &
worker_pid=$!

python -m core.cli serve --host 0.0.0.0 --port "${PORT:-8000}" &
api_pid=$!

# Exit as soon as *either* stops, rather than waiting for both. `wait -n` is
# not in POSIX sh, so this polls: a second of latency on a restart is worth not
# depending on bash being present in a slim image.
while true; do
    if ! kill -0 "$worker_pid" 2>/dev/null; then
        echo "worker exited; stopping so the platform restarts this container" >&2
        kill "$api_pid" 2>/dev/null || true
        wait "$worker_pid" || exit 1
        exit 1
    fi
    if ! kill -0 "$api_pid" 2>/dev/null; then
        echo "api exited; stopping so the platform restarts this container" >&2
        kill "$worker_pid" 2>/dev/null || true
        wait "$api_pid" || exit 1
        exit 1
    fi
    sleep 1
done
