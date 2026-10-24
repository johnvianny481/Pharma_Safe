# ADR 0008: Database access, roles and migrations

- Status: Accepted
- Date: 2026-10-25

## Decision

- **SQLAlchemy 2 (sync) with psycopg 3.** FastAPI runs sync endpoints in a thread pool, and Temporal activities (Phase 4) run sync code just as easily. Async SQLAlchemy would add complexity without a measured need at this volume. Revisit if the p95 API target (300 ms) is at risk.
- **Hand-written Alembic migrations** in `apps/api/migrations`. RLS policies, grants and triggers are the substance of the schema, and autogenerate cannot express them. The ORM classes in `pharmasafe_platform.models` only map the schema.
- **Three database roles:**

  | Role | Used by | Rights |
  |---|---|---|
  | owner (`PHARMASAFE_MIGRATION_DATABASE_URL`) | migrations, operator seeding | owns tables |
  | `pharmasafe_app` (NOLOGIN group) | — | the grants that migrations give |
  | login role in `pharmasafe_app` (`PHARMASAFE_DATABASE_URL`) | api and workers | inherits the group; RLS applies |

  Locally, `deploy/postgres/init/02-app-role.sh` creates the login role. In Azure, Terraform will (Phase 10).
- **Tests use real PostgreSQL.** Each session creates a throwaway database, migrates it and connects as a fresh login role in `pharmasafe_app`. Tests skip locally without a server and fail in CI.

## Consequences

- Adding a tenant table means writing its RLS policy and grants in the migration. The catalog test fails if this is forgotten.
