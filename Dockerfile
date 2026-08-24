# The Python image: API, worker, and migrations all run from it.
#
# One image for three roles rather than three images. They share every
# dependency and every line of application code -- the only difference is the
# command -- so separate images would mean three things to build, three to push,
# and three chances for the worker to be running code the API is not.
#
# Multi-stage, because the build needs a compiler and the runtime does not.
# Shipping build tooling in a production image adds attack surface for no
# benefit, and the layer it lives in is larger than the application.

# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS build

# Pinned to the interpreter the project is developed and tested on. "3-slim"
# would silently become 3.14 one morning and the first anyone would know is a
# dependency failing to build.

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /build

# Compilers for the wheels that need them -- argon2-cffi and asyncpg both build
# native extensions when a wheel is unavailable for the platform.
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

# Dependency metadata first, so a change to application code does not
# invalidate the layer that installs three hundred megabytes of packages. This
# ordering is most of the difference between a ten-second rebuild and a
# four-minute one.
COPY pyproject.toml README.md ./
COPY core/__init__.py core/__init__.py
COPY apps/__init__.py apps/__init__.py
COPY infrastructure/__init__.py infrastructure/__init__.py

RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --upgrade pip \
    && /opt/venv/bin/pip install .

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------
FROM python:3.13-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH="/opt/venv/bin:$PATH"

# curl is here for the health check and nothing else. A health check that
# cannot run is a container that reports healthy while being unable to serve.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root. A container process running as root that is compromised is root
# inside the container, and one kernel bug away from root outside it. Created
# before the copy so the application files can be owned by it.
RUN useradd --create-home --uid 10001 deeptrace

COPY --from=build /opt/venv /opt/venv

WORKDIR /app
COPY --chown=deeptrace:deeptrace core/ core/
COPY --chown=deeptrace:deeptrace apps/ apps/
COPY --chown=deeptrace:deeptrace infrastructure/ infrastructure/
COPY --chown=deeptrace:deeptrace alembic.ini pyproject.toml README.md ./

# The JSONL run recorder writes here when no database is configured.
RUN mkdir -p /app/data/runs && chown -R deeptrace:deeptrace /app/data

USER deeptrace

# No default CMD that guesses. Each service in compose states its own command,
# because a default of "serve" would mean a worker started by mistake silently
# becomes a second API and the queue is never drained.
