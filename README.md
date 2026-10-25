# PharmaSafe

PharmaSafe is a multi-tenant SaaS platform for pharmacovigilance. It takes adverse event reports from any channel and turns them into validated, coded, regulator-ready E2B(R3) safety cases.

**Core principle: AI proposes, humans decide.** Deterministic code handles validity, dates, regulatory clocks and E2B business rules. AI only reads text and proposes values, and each value comes with an evidence span and a confidence score. No AI output becomes part of an approved case without a signed human action.

The full product architecture and phased build plan are in [`PharmaSafe Product Architecture and Build Prompt.pdf`](PharmaSafe%20Product%20Architecture%20and%20Build%20Prompt.pdf).

> Development uses synthetic or public data only. MedDRA and WHODrug are licensed and must never be committed.

## Status

| Phase | Scope | Status |
|---|---|---|
| 0 | Foundations: monorepo, tooling, local stack, CI | **Done** |
| 1 | Identity, tenancy, audit, e-signatures | **Done** |
| 2 | Case domain | **Done** |
| 3 | Intake | **Done** |
| 4 | Workflow | **Done** |
| 5 | AI gateway and AI pipeline | **Done** |
| 6 | Review UI | Next |
| 7–10 | Submission → validation pack | Planned |

## Repository layout

```
apps/
  api/                FastAPI modular monolith (identity, tenancy, cases, intake, review, admin, reporting, audit)
  workflow_worker/    Temporal case workflow and regulatory clocks
  ai_worker/          Temporal activities for the AI pipeline
  ai_gateway/         The only path to LLM providers
  submission_worker/  E2B(R3) generation, validation, gateways, acknowledgments
  review_ui/          React + TypeScript + Vite + TanStack Query SPA
packages/
  domain/             E2B-aligned case model and deterministic rules (pure, mypy strict, 85% coverage)
  platform/           Settings, JSON logging, auth, tenancy, audit, e-signatures, telemetry, outbox
config/ai_bundles/    Signed AI configuration bundles (prompts, pinned models, thresholds)
eval/                 Datasets, harness and reports for AI evaluation (Phase 8)
tools/synthetic_data/ Synthetic report generator (Phase 8)
infra/terraform, infra/helm   Azure infrastructure (Phase 10)
deploy/               docker-compose local stack
docs/adr, docs/validation, docs/runbooks
```

## Prerequisites

- Docker Desktop (Compose v2.20+ for `--wait`)
- [uv](https://docs.astral.sh/uv/) (installs Python 3.12 automatically)
- Node.js 22+
- GNU make (optional; on Windows, use `scripts/dev.ps1 <target>`)

## Quick start

```bash
make install   # uv sync --frozen + npm ci
make up        # creates .env from .env.example, starts the stack, waits until healthy
make migrate   # apply database migrations
make seed      # demo tenants matching the local Keycloak users
make ci        # lint, typecheck, tests, security scans, SBOMs (same as CI)
make api       # API on http://localhost:8000/healthz
```

Windows without make:

```powershell
./scripts/dev.ps1 install
./scripts/dev.ps1 up
./scripts/dev.ps1 migrate
./scripts/dev.ps1 seed
./scripts/dev.ps1 ci
```

The UI runs with `cd apps/review_ui && npm run dev` on http://localhost:5173. It proxies `/api` to the API. Sign in with a demo user from the [runbook](docs/runbooks/local-development.md#demo-users).

## Local services

| Service | URL / port | Purpose |
|---|---|---|
| PostgreSQL 16 + pgvector | `localhost:5432` | App DB (`pharmasafe`), plus Temporal, Keycloak and Langfuse DBs |
| Temporal | `localhost:7233` | Durable workflows and regulatory clocks |
| Temporal UI | http://localhost:8233 | Workflow inspection |
| Azurite (Blob) | `localhost:10000` | Azure Blob Storage emulator for immutable sources |
| Keycloak | http://localhost:8180 | Local OIDC provider (admin credentials in `.env`) |
| GreenMail | SMTP `3025`, IMAP `3143`, API http://localhost:8086 | Local mail server for email intake |
| Langfuse | http://localhost:3000 | Self-hosted LLM tracing (login from `.env`) |

All ports can be overridden in `.env`. See [docs/runbooks/local-development.md](docs/runbooks/local-development.md).
