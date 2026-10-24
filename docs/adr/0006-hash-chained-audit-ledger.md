# ADR 0006: Hash-chained, append-only audit ledger

- Status: Accepted
- Date: 2026-10-25

## Context

21 CFR 11.10(e) and EU Annex 11 §9 require a secure, computer-generated, time-stamped audit trail. It must record who did what and when, the old and new values, and the reason, and no user can change it. The build prompt adds that the trail must be tamper-evident and hash-chained, and that a job verifies the chain.

## Decision

**One chain per tenant.** Each entry stores a per-tenant `seq`, the previous entry's hash (`prev_hash`) and its own `hash`. The hash is the SHA-256 of a canonical JSON encoding of every field plus `prev_hash`. A per-tenant chain fits RLS, because a tenant session can only see its own entries. It also lets tenants be verified, exported and archived independently.

**Appends are serialised per tenant** with `pg_advisory_xact_lock`. `UNIQUE (tenant_id, seq)` is a second guard. Appends happen inside the caller's transaction, so a change and its audit entry commit or roll back together.

**Hashes are computed in the application** so that the verifier can be pure and portable. Values pass through `jsonb` before hashing, because `jsonb` normalises some numbers (for example `1e20`). The hash therefore covers exactly what will be read back.

**Immutability has three layers:**

1. The app role has only `SELECT, INSERT`.
2. `BEFORE UPDATE OR DELETE` and `BEFORE TRUNCATE` triggers reject changes from every role, the owner included.
3. The hash chain makes any change made by bypassing layers 1 and 2 (for example a superuser disabling triggers) detectable. Tests simulate exactly that.

**Verification.** `verify_chain` checks sequence continuity, `prev_hash` links and recomputed hashes, and reports the first bad entry. `python -m pharmasafe_api.audit.verify_job` verifies every tenant and exits non-zero on any break. Phase 4 schedules it nightly as a Temporal schedule. Phase 10 archives verified chain heads to WORM storage.

## Consequences

- Removing the newest entries cannot be detected from the chain alone. The WORM archive of chain heads (Phase 10) closes that gap.
- Writes for one tenant are serialised. This is fine at the target volume of about 100k cases per tenant per month. If it ever becomes a bottleneck, the chain can be partitioned per case.
