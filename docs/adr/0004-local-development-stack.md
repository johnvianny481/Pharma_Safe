# ADR 0004: Local development stack

- Status: Accepted
- Date: 2026-10-25

## Context

Developers need the platform's dependencies running locally. These are Postgres with pgvector, Temporal, Blob storage, an OIDC provider, a mail server and LLM tracing. Each should be a close stand-in for the Azure service used in production.

## Decision

`deploy/docker-compose.yml` runs the stack below. Every image is pinned to an exact version, and every service has a healthcheck, so `make up` (`docker compose up --wait`) only returns once the stack is usable.

| Local | Production counterpart |
|---|---|
| `pgvector/pgvector:0.8.0-pg16` | Azure Database for PostgreSQL Flexible Server + pgvector |
| `temporalio/auto-setup:1.25.2` + UI | Temporal (self-hosted on AKS or Temporal Cloud) |
| Azurite 3.33 (Blob only) | Azure Blob Storage with immutability policies |
| Keycloak 26.0 (`start-dev`) | Microsoft Entra ID and customer IdPs (OIDC/SAML) |
| GreenMail 2.1 | Microsoft Graph / IMAP mailbox |
| Langfuse 2.95 | Self-hosted Langfuse |

Other choices:

- **One Postgres instance** hosts the app, Temporal, Keycloak and Langfuse databases. An init script creates the extra databases and enables `vector` and `pgcrypto` in `pharmasafe`. This keeps memory use low on laptops. Production keeps them separate.
- **Langfuse v2 rather than v3.** v2 runs as a single container on Postgres. v3 also needs ClickHouse, Redis and S3-compatible storage. That suits production but is heavy for a laptop. The client SDK API is the same, so moving to v3 is a deployment change only. Revisit in Phase 9.
- **The Service Bus emulator is deferred.** The build prompt allows a Postgres-backed queue in development. The transactional outbox arrives in Phase 2, and the local transport will be chosen then.
- **Healthchecks without curl.** The Keycloak and GreenMail images ship no HTTP client, so their healthchecks probe HTTP endpoints through bash's `/dev/tcp`.
- **Credentials** come from `.env`, which is git-ignored and created from `.env.example`. Compose fails fast (`${VAR:?}`) if a required value is missing.

## Consequences

- `make up` takes about 2–3 minutes on first start, mostly waiting on Keycloak and Langfuse.
- The stack needs about 4 GB RAM for Docker.
