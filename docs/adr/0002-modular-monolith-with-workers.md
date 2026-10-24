# ADR 0002: Modular monolith plus workers

- Status: Accepted
- Date: 2026-10-25

## Context

The product needs an API, durable case workflows, AI processing, LLM access control and regulator submissions. Running each as a separate microservice would multiply validation, deployment and operations work for a small team. Some parts, though, will need to scale on their own, the AI workers in particular.

## Decision

The system has six deployable units:

| Unit | Role |
|---|---|
| `api` | FastAPI modular monolith: identity, tenancy, cases, intake, review, admin, reporting, audit |
| `workflow-worker` | Temporal workers for the case workflow and regulatory clocks |
| `ai-worker` | Temporal activities for the AI pipeline |
| `ai-gateway` | The only service allowed to call LLM providers |
| `submission-worker` | E2B(R3) generation, validation, gateway connectors, acknowledgments |
| `review-ui` | React SPA |

Module boundaries are strict:

- A module never reads another module's tables. It uses that module's public interface or consumes its outbox events.
- Shared code lives in two packages only. `packages/domain` holds pure rules and the case model, with no I/O. `packages/platform` holds cross-cutting infrastructure.

## Consequences

- There is one codebase and one language (Python) for the backend, and few deployables to validate.
- A busy module can be split into its own service later without a redesign.
- Boundary discipline relies on review for now. An import-linter contract can enforce it once real modules exist (Phase 2).
