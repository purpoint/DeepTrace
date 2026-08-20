.DEFAULT_GOAL := help
.PHONY: help setup install lint format typecheck test test-cov check clean run

VENV   := .venv
PYTHON := $(VENV)/bin/python
PIP    := $(VENV)/bin/pip
RUFF   := $(VENV)/bin/ruff
MYPY   := $(VENV)/bin/mypy
PYTEST := $(VENV)/bin/pytest

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

test: ## Run tests, excluding those that make paid API calls
	$(PYTEST) -m "not llm"

test-cov: ## Run tests with a coverage report
	$(PYTEST) -m "not llm" --cov=core --cov=apps --cov=infrastructure --cov-report=term-missing

check: lint typecheck test ## Run everything CI runs
	@echo "All checks passed."

run: ## Show resolved configuration
	$(PYTHON) -m core.cli status

clean: ## Remove caches and build artifacts
	find . -type d -name __pycache__ -not -path "./$(VENV)/*" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage dist build *.egg-info
