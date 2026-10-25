# ADR 0011: Transactional outbox

- Status: Accepted
- Date: 2026-10-25

## Context

Modules communicate through events, and other services must never miss an event or see one for a change that was rolled back. Production uses Azure Service Bus. Development can use an emulator or a Postgres-backed queue.

## Decision

- `pharmasafe_platform.outbox.enqueue` writes the event to the `outbox` table in the caller's transaction, so it commits or rolls back with the change. Payloads carry identifiers and codes only, never personal data.
- `OutboxRelay.run_once` goes tenant by tenant, under RLS, through a tenant session. It does the following:
  - claims pending rows in `seq` order with `FOR UPDATE SKIP LOCKED`;
  - publishes each row through a `Publisher`;
  - marks the row published.
  - On a failure, it records `attempts` and `last_error` and stops that tenant's batch, so later events do not overtake an earlier one.
- Delivery is **at least once**. Consumers deduplicate on the event `id`.
- The app role may insert and read outbox rows, and update only `published_at`, `attempts` and `last_error`. It cannot delete them. Retention is an operator job.

Case events now in use:

- `case.created`
- `case.clock_started`
- `case.version_approved`
- `case.follow_up_started`

## Consequences

- Phase 2 ships the outbox, the relay and an in-memory publisher. The Service Bus publisher and the process that runs the relay come with the first consumer, the Phase 4 workflow worker. Building the broker adapter before anything consumes it would be speculative.
- Ordering is guaranteed per tenant for a single relay. With several relay replicas, ordering is best effort, and consumers must not depend on it across aggregates.
