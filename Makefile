.DEFAULT_GOAL := help
VENV ?= .venv
PYTHON ?= python3
PY := $(VENV)/bin/python
BIN := $(VENV)/bin

.PHONY: help install init run test lint format typecheck check demo clean

help:  ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-12s %s\n", $$1, $$2}'

install:  ## Create .venv and install the package with dev tools
	$(PYTHON) -m venv $(VENV)
	$(BIN)/pip install --upgrade pip
	$(BIN)/pip install -e ".[dev]"

init:  ## Create .env (random secrets) and config/risk.yaml if missing
	$(PY) scripts/init_env.py

run:  ## Start the bridge on 127.0.0.1:8000 with SimBroker (default)
	$(BIN)/tv-bridge serve

test:  ## Run the pytest suite
	$(BIN)/pytest

lint:  ## ruff lint + format check
	$(BIN)/ruff check .
	$(BIN)/ruff format --check .

format:  ## Auto-format and fix lint issues
	$(BIN)/ruff check --fix .
	$(BIN)/ruff format .

typecheck:  ## mypy --strict
	$(BIN)/mypy

check: lint typecheck test  ## Everything CI runs

demo:  ## Send sample alerts (including a duplicate) to a running bridge
	$(PY) scripts/demo_alerts.py

clean:  ## Remove caches and local state
	rm -rf .pytest_cache .mypy_cache .ruff_cache build dist *.egg-info data logs
