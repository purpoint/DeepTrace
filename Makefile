.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test test-int test-cov check clean run \
	db-up db-down db-reset db-revision api worker web web-install web-check \
	up down destroy logs secrets tls-cert up-deploy down-deploy verify-deploy

VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
RUFF   := $(VENV)/bin/ruff
MYPY   := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest
ALEMBIC := $(VENV)/bin/alembic

help: ## Show available targets
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Create the virtualenv, install dependencies, and write .env
	python3 -m venv $(VENV)
	$(PIP) install --upgrade pip --quiet
	$(PIP) install -e ".[dev]" --quiet
	@test -f .env || (cp .env.example .env && echo "Created .env from template -- add your API keys")
	@echo "Setup complete. Run 'make check' to verify."

install: ## Reinstall dependencies into an existing virtualenv
	$(PIP) install -e ".[dev]" --quiet

lint: ## Run ruff, including the formatting check CI enforces
	$(RUFF) check .
	$(RUFF) format --check .

format: ## Format code and fix what ruff can fix automatically
	$(RUFF) format .
	$(RUFF) check --fix .

typecheck: ## Run mypy
	$(MYPY) core apps infrastructure

test: ## Run unit tests (no database, no network, no paid API calls)
	$(PYTEST) -m "not llm and not integration"

test-int: ## Run integration tests against PostgreSQL
	$(PYTEST) -m integration

test-cov: ## Run tests with a coverage report
	$(PYTEST) -m "not llm" --cov=core --cov=apps --cov=infrastructure --cov-report=term-missing

check: lint typecheck test ## Run everything CI runs
	@echo "All checks passed."

run: ## Show resolved configuration
	$(PYTHON) -m core.cli status

api: ## Run the HTTP API with reload
	$(PYTHON) -m core.cli serve --reload

worker: ## Run a research worker
	$(PYTHON) -m core.cli work

web: ## Run the browser client (expects the API on :8000)
	cd apps/web && npm run dev

web-install: ## Install the browser client's dependencies
	cd apps/web && npm install

web-check: ## Typecheck, test, and build the browser client
	cd apps/web && npx tsc --noEmit && npx vitest run && npm run build

db-up: ## Apply all pending migrations
	$(ALEMBIC) upgrade head

db-down: ## Reverse the most recent migration
	$(ALEMBIC) downgrade -1

db-reset: ## Drop everything and rebuild from migrations
	$(ALEMBIC) downgrade base
	$(ALEMBIC) upgrade head

db-revision: ## Generate a migration from model changes: make db-revision m="what changed"
	$(ALEMBIC) revision --autogenerate -m "$(m)"

up: ## Build and start the whole stack in containers
	docker compose up --build -d
	@echo "Client on http://localhost:$${WEB_PORT:-8080}"

down: ## Stop the stack, keeping the database and queue volumes
	docker compose down

destroy: ## Stop the stack and delete its data. Not reversible.
	docker compose down --volumes

logs: ## Follow the logs of every service
	docker compose logs -f

# ---------------------------------------------------------------------------
# Deployment: TLS at the edge, secrets from files
# ---------------------------------------------------------------------------

secrets: ## Generate deploy/secrets/* for the deployment overlay
	@mkdir -p deploy/secrets
	@if [ -e deploy/secrets/jwt_secret ]; then \
		echo "deploy/secrets already populated. Delete it to regenerate."; \
		echo "Regenerating rotates every credential at once, which is rarely what you meant."; \
		exit 1; \
	fi
	@python3 -c "import secrets; print(secrets.token_urlsafe(48), end='')" > deploy/secrets/jwt_secret
	@python3 -c "import secrets; print(secrets.token_urlsafe(24), end='')" > deploy/secrets/postgres_password
	@printf 'postgresql+asyncpg://%s:%s@postgres:5432/%s' \
		"$${POSTGRES_USER:-deeptrace}" \
		"$$(cat deploy/secrets/postgres_password)" \
		"$${POSTGRES_DB:-deeptrace}" > deploy/secrets/database_url
	@touch deploy/secrets/google_api_key deploy/secrets/tavily_api_key
	# The directory is the boundary, not the file mode. `chmod 600` here looks
	# stricter and is actually broken: compose bind-mounts these files, the
	# application container runs as uid 10001, and a file owned by the deploying
	# user with mode 600 is unreadable to it -- the container refuses to start,
	# correctly, saying it cannot read a secret that is right there. 0700 on the
	# directory stops every other user on the host from traversing into it,
	# which is the protection that was wanted; the container reaches the file
	# through its own mount and never walks that path.
	@chmod 700 deploy/secrets
	@chmod 644 deploy/secrets/*
	@echo "Wrote deploy/secrets/. The database URL is built from the password file,"
	@echo "so the two cannot disagree -- do not edit either by hand."
	@echo
	@echo "Two are deliberately empty. Put your keys in them:"
	@echo "  deploy/secrets/google_api_key"
	@echo "  deploy/secrets/tavily_api_key"

tls-cert: ## Generate a self-signed certificate for verifying the TLS stack
	@mkdir -p deploy/certs
	openssl req -x509 -nodes -newkey rsa:2048 -days 365 \
		-keyout deploy/certs/privkey.pem \
		-out deploy/certs/fullchain.pem \
		-subj "/CN=$${TLS_HOST:-localhost}" \
		-addext "subjectAltName=DNS:$${TLS_HOST:-localhost}"
	@chmod 700 deploy/certs
	@chmod 644 deploy/certs/privkey.pem
	@echo
	@echo "Self-signed, and only good for proving the stack terminates TLS."
	@echo "A browser will refuse it, correctly. A real deployment replaces both"
	@echo "files with a certificate from a CA and reloads nginx."

verify-deploy: ## Bring the stack up and prove it serves over TLS, end to end
	@bash scripts/verify-deploy.sh

up-deploy: ## Start the stack with TLS and file-based secrets
	docker compose -f docker-compose.yml -f docker-compose.deploy.yml up --build -d
	@echo "Client on https://localhost:$${HTTPS_PORT:-8443}"

down-deploy: ## Stop the deployment stack
	docker compose -f docker-compose.yml -f docker-compose.deploy.yml down

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build *.egg-info
