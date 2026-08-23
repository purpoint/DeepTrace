.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test test-int test-cov check clean run \
	db-up db-down db-reset db-revision api worker web web-install web-check

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

lint: ## Run ruff
	$(RUFF) check .

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

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build *.egg-info
