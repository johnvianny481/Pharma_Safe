# PharmaSafe developer commands. On Windows without make, use: scripts/dev.ps1 <target>
.DEFAULT_GOAL := help
SHELL := bash

COMPOSE := docker compose --env-file .env -f deploy/docker-compose.yml
PY_SRC := packages/domain/src packages/platform/src apps/api/src apps/ai_gateway/src \
          apps/workflow_worker/src apps/ai_worker/src apps/submission_worker/src
UI := apps/review_ui
SBOM_NPM := @cyclonedx/cyclonedx-npm@6.0.1

.PHONY: help env up down reset logs ps sync install lint fmt typecheck test test-domain \
        security sbom ui-install ci migrate seed verify-audit api ai-gateway email-intake \
        workflow-worker ai-worker

help: ## List targets
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  %-12s %s\n",$$1,$$2}'

env: ## Create .env from .env.example if missing
	@test -f .env || (cp .env.example .env && echo "Created .env from .env.example")

up: env ## Start the local stack and wait until every service is healthy
	$(COMPOSE) up -d --wait --wait-timeout 600
	@$(COMPOSE) ps

down: ## Stop the local stack (keeps volumes)
	$(COMPOSE) down

reset: ## Stop the stack and delete its volumes
	$(COMPOSE) down -v

logs: ## Tail logs from the local stack
	$(COMPOSE) logs -f --tail=100

ps: ## Show service status
	$(COMPOSE) ps

sync: ## Install Python deps (locked)
	uv sync --frozen

ui-install: ## Install UI deps (locked)
	cd $(UI) && npm ci

install: sync ui-install ## Install everything

lint: ## Lint Python and UI
	uv run ruff check .
	uv run ruff format --check .
	cd $(UI) && npm run lint

fmt: ## Auto-format Python
	uv run ruff check --fix .
	uv run ruff format .

typecheck: ## mypy (strict) and tsc
	uv run mypy $(PY_SRC)
	cd $(UI) && npm run typecheck

test-domain: ## Domain tests with the 85% coverage gate
	uv run pytest packages/domain --cov=pharmasafe_domain --cov-fail-under=85

test: test-domain ## All tests
	uv run pytest
	cd $(UI) && npm test

security: ## Static analysis and dependency audit
	mkdir -p reports
	uv run bandit -c pyproject.toml -r packages apps -q
	uv export --frozen --no-dev --no-emit-workspace --no-hashes -o reports/requirements.txt -q
	uv run pip-audit --strict -r reports/requirements.txt
	cd $(UI) && npm audit --audit-level=high

sbom: ## CycloneDX SBOMs for Python and UI
	mkdir -p sbom
	uv export --frozen --no-dev --no-emit-workspace --no-hashes -o sbom/python-requirements.txt -q
	uv run cyclonedx-py requirements sbom/python-requirements.txt --of JSON -o sbom/python.cdx.json
	cd $(UI) && npx --yes $(SBOM_NPM) --omit dev --output-file ../../sbom/review-ui.cdx.json

ci: lint typecheck test security sbom ## Everything CI runs

migrate: ## Apply database migrations (as the schema owner)
	uv run alembic -c apps/api/alembic.ini upgrade head

seed: ## Create the demo tenants that match the local Keycloak users
	uv run python -m pharmasafe_api.tenancy.seed

verify-audit: ## Verify every tenant's audit hash chain
	uv run python -m pharmasafe_api.audit.verify_job

email-intake: ## Poll the tenants' intake mailboxes (GreenMail locally)
	uv run python -m pharmasafe_api.intake.email_intake

workflow-worker: ## Run the Temporal case workflow worker and the outbox relay
	uv run python -m pharmasafe_workflow_worker

ai-worker: ## Run the AI pipeline worker (model calls go through the AI gateway)
	uv run python -m pharmasafe_ai_worker

api: ## Run the API locally on :8000
	uv run uvicorn pharmasafe_api.main:app --reload --port 8000

ai-gateway: ## Run the AI gateway locally on :8001
	uv run uvicorn pharmasafe_ai_gateway.main:app --reload --port 8001
