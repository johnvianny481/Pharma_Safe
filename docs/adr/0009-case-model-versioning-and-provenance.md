# ADR 0009: Case model, versioning and field provenance

- Status: Accepted
- Date: 2026-10-25

## Context

The build prompt requires:

- a case model that mirrors the main E2B(R3) sections;
- approved versions that never change, with follow-up information creating a new version;
- every field storing its value, source (AI, human or import), evidence span, confidence, and who confirmed it and when;
- every case version can be rebuilt as it existed on any past date.

## Decision

**Model.** `pharmasafe_domain.case.SafetyCase` is a frozen Pydantic v2 model. Its sections follow E2B(R3): C.1 admin, C.2.r reporters, D patient, E.i reactions, F.r tests, G.k drugs, G.k.9.i.2.r causality, and H narrative. Each data field is a `Tracked[T]` holding value, source, evidence, confidence, `confirmed_by` and `confirmed_at`, or `None` if unknown. Seriousness has six criteria per reaction (E.i.3.2a–f), each a separate tracked field.

- *Why Pydantic in the domain package.* It is already a platform dependency. It gives strict validation and an exact JSON form, and versioning and signatures hash that form. Numbers are `Decimal`, which serialises as a string, so values survive `jsonb` exactly.
- *Structural vs plausibility rules.* The model rejects values that are structurally impossible, such as an unknown code or a negative age. `rules.checks` flags values that are possible but suspicious, so a reviewer sees them.

**Provenance invariants** (enforced in `Tracked`):

- An AI value without both evidence and confidence is rejected.
- A human value records who entered it.
- `confirmed_by` and `confirmed_at` go together.

The API never accepts provenance from clients. A value set through the API is `human` and confirmed by the caller. `confirm` keeps a proposed value and its evidence and adds the confirmer.

**Changes are operations on paths:**

- `set /patient/age`
- `add /reactions`
- `remove /reactions/r1`

List items are addressed by a stable `id`, not by position. `changes.apply_change` is the only way content changes.

**Versions.** `case_versions` holds one row per version with `status` (`draft` or `approved`), the content as `jsonb`, `content_hash` and `revision`. The database enforces the following, for every role including the owner:

- An approved row cannot be updated.
- No row can be deleted.
- Each draft update must raise `revision` by exactly 1, which gives optimistic concurrency.
- A case has at most one draft.

Approval needs an e-signature with meaning *Approved*, bound to the version's content hash (ADR 0007). A follow-up copies the latest approved version into a new draft. Its first change sets the follow-up receipt date.

**Rebuild from the audit ledger.** Each version starts with a `case.version_created` audit entry holding its base content. Every change is its own entry (path, old value, new value, reason, case version). `rebuild_version` replays these entries, up to an optional `as_of`, with the same `apply_change` function. For the current state, the rebuild's hash must equal the stored `content_hash`. A property-based test runs random edit scripts against PostgreSQL and checks that every intermediate state rebuilds exactly.

**Approval blockers** (`rules.checks.approval_blockers`):

- any `error` issue from the date and unit checks;
- a failed validity criterion;
- a missing receipt date;
- any unconfirmed value;
- any seriousness criterion not yet assessed.

**Data scopes.** `cases.region` comes from the occurrence country (EEA countries map to `eu`, then `us`, `in`, `other`, `unknown`). `cases.product_codes` lists the suspect and interacting drugs' company product codes. Queries filter by the user's scope. A case is visible if its region is in scope and at least one of its products is in scope. A case with no known region or product is visible only to users with unrestricted scope (fail closed). An out-of-scope case returns 404, as an absent one does.

## Consequences

- The content of an approved version, its hash and its signature can only be read, never changed. Corrections need a follow-up version.
- Approval enforces step order and blockers for now. Phase 4 moves the order of steps (four-eyes, quality and medical review) into the Temporal workflow.
- The JSON schema is versioned (`schema_version`). A future change to it needs a migration that keeps old versions readable as they were.
