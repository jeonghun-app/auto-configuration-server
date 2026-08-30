.DEFAULT_GOAL := help
PYTHON ?= .venv/bin/python
VENV := .venv
COVERAGE_MIN ?= 88

.PHONY: help venv install fmt lint types test test-cov check coverage-doc privacy \
        docker-build docker-run docker-stop up down verify infra-lint clean

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

venv: ## Create the virtual environment
	python3.11 -m venv $(VENV)
	$(VENV)/bin/pip install --upgrade pip

install: venv ## Install runtime and development dependencies
	$(VENV)/bin/pip install -r requirements-dev.txt

fmt: ## Format the code
	$(VENV)/bin/ruff format src tests tools scripts
	$(VENV)/bin/ruff check --fix src tests tools scripts

lint: ## Check formatting and lint rules
	$(VENV)/bin/ruff format --check src tests tools scripts
	$(VENV)/bin/ruff check src tests tools scripts

types: ## Type-check with mypy in strict mode
	$(VENV)/bin/mypy

test: ## Run the test suite
	$(PYTHON) -m pytest

test-cov: ## Run the test suite with a coverage gate
	$(PYTHON) -m pytest --cov=acs --cov-report=term-missing \
	  --cov-fail-under=$(COVERAGE_MIN)

infra-lint: ## Validate the CloudFormation templates and shell scripts
	$(VENV)/bin/cfn-lint infra/ecr.yaml infra/app.yaml
	@command -v shellcheck >/dev/null && shellcheck scripts/*.sh || \
	  echo "shellcheck not installed; skipping"

privacy: ## Refuse committed subscriber identifiers
	./scripts/check_identifiers.sh

coverage-doc: ## Regenerate docs/spec-coverage.md from the catalogues
	$(PYTHON) scripts/gen_spec_coverage.py

check: lint types test-cov infra-lint privacy ## Everything CI runs
	$(PYTHON) scripts/gen_spec_coverage.py --check

docker-build: ## Build the container image
	docker build -t rcs-acs:local .

docker-run: docker-build ## Run the container locally on port 8080
	docker rm -f rcs-acs-local 2>/dev/null || true
	docker run -d --name rcs-acs-local -p 8080:8080 \
	  -e ACS_ENV=dev \
	  -e ACS_STORE_BACKEND=memory \
	  -e ACS_SMS_PROVIDER=mock \
	  -e ACS_DEV_ENDPOINTS_ENABLED=true \
	  -e ACS_ADMIN_TOKEN=local-admin-token \
	  rcs-acs:local

docker-stop: ## Stop the local container
	docker rm -f rcs-acs-local 2>/dev/null || true

up: ## Start the full local stack (ACS + DynamoDB Local)
	docker compose up --build -d

down: ## Stop the local stack and remove its volumes
	docker compose down -v

verify: ## Run the end-to-end verification against a running ACS
	ACS_BASE_URL=$${ACS_BASE_URL:-http://127.0.0.1:8080} \
	ACS_ADMIN_TOKEN=$${ACS_ADMIN_TOKEN:-local-admin-token} \
	  $(PYTHON) scripts/verify_stack.py

clean: ## Remove caches and build artefacts
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage dist build
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
