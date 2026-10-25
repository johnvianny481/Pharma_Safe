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
| Poll intake mailboxes | `make email-intake` | `./scripts/dev.ps1 email-intake` |
| Run the case workflow worker | `make workflow-worker` | `./scripts/dev.ps1 workflow-worker` |
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

## Case API

The OpenAPI docs are at http://localhost:8000/docs once the API is running. A case's life:

1. `POST /cases` with `changes` (a case processor or intake processor).
2. `PATCH /cases/{id}/versions/1` with `expected_revision` and more `changes`. Operations are `set`, `confirm`, `add` and `remove` on paths such as `/patient/initials`, `/reactions`, `/reactions/r1/seriousness/death`.
3. `GET /cases/{id}/versions/1/assessment` shows validity, seriousness, date and unit issues, approval blockers, day 0 and regulatory clocks.
4. `POST /cases/{id}/versions/1/approve` (a quality or medical reviewer) with `reauth_token`, which is an access token from a fresh login.
5. `POST /cases/{id}/follow-ups` with the follow-up `receipt_date` creates version 2.
6. `GET /cases/{id}/versions/1/rebuild?as_of=...` (a tenant admin or quality reviewer) rebuilds a version from the audit trail.

Clock rules are configurable **examples** in `packages/domain/src/pharmasafe_domain/rules/rule_sets/` (ADR 0010). A tenant's regulators are in `tenants.regulators`, and the default is FDA and EMA.

## Intake

Every intake POST needs an `Idempotency-Key` header, such as a UUID. Send the same key again when retrying.

- `POST /intake/reports` takes JSON `{"text": ...}`, from customer systems.
- `POST /intake/forms` takes the web/call-centre form. The review UI shows it to intake and case processors.
- `POST /intake/uploads` takes multipart `files`: PDFs, images, text and `.eml`.
- `GET /intake/submissions/{id}` returns the acknowledgment, the documents, and the cases created.
- `GET /intake/documents/{id}/content` returns the original bytes, verified against their SHA-256.

**Email.** `make seed` gives Acme the mailbox `intake@acme.pharmasafe.local` and Globex `intake@globex.pharmasafe.local`. Send a message to GreenMail on SMTP port 3025, for example with `swaks` or Python's `smtplib`, then run `make email-intake` (`--once` for a single pass).

**OCR.** Images are read with Tesseract when `tesseract` is on the PATH (or set `PHARMASAFE_TESSERACT_CMD`). Without it, images and scanned PDFs are stored and marked for manual entry. On Windows, install it with `winget install UB-Mannheim.TesseractOCR` and set `PHARMASAFE_TESSERACT_CMD=C:/Program Files/Tesseract-OCR/tesseract.exe` in `.env`. OCR uses English, German, French, Spanish and Italian when their language data is installed. The API logs a warning at startup for any that are missing. The Windows installer adds only English by default. Copy the others' `.traineddata` files from [tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) into its `tessdata` folder.

**Sources** go to the `sources` container in Azurite, one blob per document named by its SHA-256.

## Case workflow and review tasks

Run `make workflow-worker` next to the API. It runs the Temporal case workflow and the outbox relay, which turns case events into workflow signals (ADR 0013). Each case is the workflow `case-{case_id}`. Inspect it in the Temporal UI at http://localhost:8233.

- `GET /tasks` is your work queue (`?mine=true` for tasks assigned to you), nearest regulatory deadline first.
- `POST /tasks/{id}/claim` or `POST /tasks/{id}/assign` with `{"assignee": ...}` assigns a task.
- `POST /tasks/{id}/complete` with `reauth_token` signs the step (Reviewed, Quality reviewed, Medically approved or Approved).
- `POST /tasks/{id}/return` with `reason` sends the case back to data entry.

The person who did data entry cannot sign a later step of the same version. To try it, sign data entry as `acme.processor`, then quality and medical review as `acme.quality` and `acme.medical`. Escalations are in `deadline_escalations` and the audit trail.

**Workflow changes.** A change to `workflows.py` that alters the commands a running workflow issues must be guarded with `workflow.patched(...)`. The replay test fails otherwise. Record a new history with `PHARMASAFE_RECORD_HISTORIES=1` and commit it with the change.

## Database roles

The API and workers connect as `pharmasafe_app_user`. It is a non-owner role, so row-level security always applies. Migrations and `seed` connect as the owner (`PHARMASAFE_MIGRATION_DATABASE_URL`). See ADR 0008.

## Troubleshooting

- **`up` times out on keycloak or langfuse.** On a slow first start, Keycloak runs a build step before it serves traffic. Run `make up` again; it picks up where it left off.
- **Port already in use.** Override the port in `.env` (for example `POSTGRES_PORT=5433`).
- **Changed `deploy/postgres/init/*` but nothing happened, or `pharmasafe_app_user` does not exist.** Init scripts only run on an empty volume. Run `make reset`, then `make up`, `make migrate` and `make seed`.
- **Changed the Keycloak realm file but nothing happened.** `--import-realm` skips realms that already exist. Run `make reset`, or delete the `pharmasafe` realm in the admin console and restart Keycloak.
- **DB tests are skipped.** Set `PHARMASAFE_TEST_ADMIN_DATABASE_URL` in `.env` (see `.env.example`) and make sure Postgres is up.
- **`.env` missing variable error from compose.** Compare `.env` with `.env.example` and add the missing key.
