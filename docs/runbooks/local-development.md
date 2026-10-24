# Runbook: local development

## Start and stop

| Task | make | Windows (no make) |
|---|---|---|
| Install deps | `make install` | `./scripts/dev.ps1 install` |
| Start stack (waits for healthy) | `make up` | `./scripts/dev.ps1 up` |
| Apply migrations | `make migrate` | `./scripts/dev.ps1 migrate` |
| Create demo tenants | `make seed` | `./scripts/dev.ps1 seed` |
| Verify audit chains | `make verify-audit` | `./scripts/dev.ps1 verify-audit` |
| Run the API (:8000) | `make api` | `./scripts/dev.ps1 api` |
| Status | `make ps` | `./scripts/dev.ps1 ps` |
| Logs | `make logs` | `./scripts/dev.ps1 logs` |
| Stop (keep data) | `make down` | `./scripts/dev.ps1 down` |
| Stop and wipe volumes | `make reset` | `./scripts/dev.ps1 reset` |
| Everything CI runs | `make ci` | `./scripts/dev.ps1 ci` |

First time: `install`, `up`, `migrate`, `seed`, `api`. Then run `cd apps/review_ui && npm run dev` and open http://localhost:5173.

## Demo users

Every user's password is `KC_DEMO_USER_PASSWORD` from `.env`. Tenant ids match `make seed`.

| Username | Tenant | Role | Scope |
|---|---|---|---|
| `acme.intake` | Acme Pharma | intake_processor | all |
| `acme.processor` | Acme Pharma | case_processor | all |
| `acme.quality` | Acme Pharma | quality_reviewer | all |
| `acme.medical` | Acme Pharma | medical_reviewer | all |
| `acme.admin` | Acme Pharma | tenant_admin | all |
| `acme.eu-only` | Acme Pharma | case_processor | product `ACM-001`, region `eu` |
| `globex.processor` | Globex Bio | case_processor | all |
| `globex.admin` | Globex Bio | tenant_admin | all |
| `platform.admin` | none | platform_admin | none |

The Keycloak admin console is at http://localhost:8180, with the admin user from `.env`.

## Database roles

The API and workers connect as `pharmasafe_app_user`. It is a non-owner role, so row-level security always applies. Migrations and `seed` connect as the owner (`PHARMASAFE_MIGRATION_DATABASE_URL`). See ADR 0008.

## Troubleshooting

- **`up` times out on keycloak or langfuse.** On a slow first start, Keycloak runs a build step before it serves traffic. Run `make up` again; it picks up where it left off.
- **Port already in use.** Override the port in `.env` (for example `POSTGRES_PORT=5433`).
- **Changed `deploy/postgres/init/*` but nothing happened, or `pharmasafe_app_user` does not exist.** Init scripts only run on an empty volume. Run `make reset`, then `make up`, `make migrate` and `make seed`.
- **Changed the Keycloak realm file but nothing happened.** `--import-realm` skips realms that already exist. Run `make reset`, or delete the `pharmasafe` realm in the admin console and restart Keycloak.
- **DB tests are skipped.** Set `PHARMASAFE_TEST_ADMIN_DATABASE_URL` in `.env` (see `.env.example`) and make sure Postgres is up.
- **`.env` missing variable error from compose.** Compare `.env` with `.env.example` and add the missing key.
