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
| Run the AI gateway (:8001) | `make ai-gateway` | `./scripts/dev.ps1 ai-gateway` |
| Run the AI pipeline worker | `make ai-worker` | `./scripts/dev.ps1 ai-worker` |
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

## AI gateway and pipeline

Run three processes next to the API, each in its own terminal:

- `make ai-gateway` serves the gateway on :8001.
- `make ai-worker` runs the pipeline on the `ai-pipeline` queue.
- `make workflow-worker` runs the case workflow, which starts the pipeline for each new case (ADR 0014).

By default everything runs on **MockLLM** (bundle `mock-2026.10.1`), so no provider keys are needed.

- `GET /cases/{id}/versions/{v}/ai` shows what the pipeline did: proposals (suggestions at L0), rejected values with reasons, both seriousness signals, duplicates with reasons, coding, follow-up questions and the narrative draft.
- AI values in the case have `source: ai`, an evidence span pointing at the source document, a confidence, and no confirmation until a person confirms them.
- The tenant's automation level is `tenants.ai_automation_level` (L0, L1 or L2); the default is L1. Set it as the owner, for example `UPDATE tenants SET ai_automation_level = 'L0' WHERE slug = 'acme-pharma'`.
- Model calls are in `ai_calls`. Redacted traces are in Langfuse at http://localhost:3000, one trace per case (`case-{id}`).
- The monthly budget is USD 500 per tenant by default. Set another one with `INSERT INTO ai_tenant_budgets VALUES ('<tenant id>', 50)`.

**Real providers.** Set `PHARMASAFE_AZURE_OPENAI_ENDPOINT`, `PHARMASAFE_AZURE_OPENAI_KEY` and `PHARMASAFE_OPENAI_API_KEY`, then `PHARMASAFE_AI_BUNDLE_VERSION=2026.10.1`. The Azure deployment must be called `pharmasafe-gpt-4o` and serve `gpt-4o-2024-08-06`. Use synthetic reports only.

**Changing prompts or settings** (change control):

1. Edit `config/ai_bundles/prompts.py`.
2. Run `uv run python config/ai_bundles/prompts.py`.
3. Re-sign each changed bundle: `uv run python -m pharmasafe_platform.aiconfig sign config/ai_bundles/<version> <private-key> <key-id>`.
4. Check it: `uv run python -m pharmasafe_platform.aiconfig verify config/ai_bundles/<version>`.

An edited but unsigned bundle is refused by every service, on purpose. A changed bundle should get a new version: running cases keep the version they started with.

**Signing keys.** The development key is at `.secrets/ai-bundle-dev-2026-10.key`. It is git-ignored; its public half is `config/ai_bundles/trusted_keys/dev-2026-10.pub`. To make your own key, run `uv run python -m pharmasafe_platform.aiconfig keygen .secrets/<id>.key config/ai_bundles/trusted_keys/<id>.pub`. Release keys belong in Key Vault, never on a laptop.

**Redaction key.** `PHARMASAFE_REDACTION_MASTER_KEY` encrypts the token mappings. The all-zero value in `.env.example` is for local use only. Losing the key makes stored mappings unreadable; the case data itself is not affected.

## Review UI

Run the API, the gateway and both workers (above), then the UI with `cd apps/review_ui && npm run dev`, and open http://localhost:5173. Sign in as a demo user (password `KC_DEMO_USER_PASSWORD` from `.env`).

- **Work queue** (`/`): your open tasks, most urgent deadline first, with AI flags.
- **Case workspace** (`/cases/{id}`): the source document on the left, with evidence highlighted, and the E2B form on the right. Confirm or correct each value, assess every seriousness criterion, then use **Sign and complete**. Signing asks for your password again in a popup, so allow popups for localhost.
- **Duplicates**: when the AI flags a possible duplicate, choose **Compare**, pick what to copy, then **Merge** (e-signed). The duplicate is closed into the master case.
- **History and audit** tab: the versions (approved ones are read-only), **Start follow-up version**, and the case's audit trail.
- **New report** (`/intake`): the intake form, for intake and case processors.
- **Administration** (`/admin`, `acme.admin`): automation level, QC sampling, AI budget, regulators, mailbox, and audit chain verification.

A typical walk-through:

1. As `acme.intake`, enter a report with a description.
2. As `acme.processor`, open it from the queue, confirm the values and sign.
3. As `acme.quality`, approve it.

With the default QC sampling (10%), some non-serious cases also get a quality review first.

### End-to-end tests (Playwright)

| Task | make | Windows (no make) |
|---|---|---|
| Install the browser once | `cd apps/review_ui && npx playwright install chromium` | same |
| Run the end-to-end tests | `make e2e` | `./scripts/dev.ps1 e2e` |

The tests need `make up`. Playwright starts the API, the gateway (MockLLM), both workers and the UI itself (`scripts/e2e_stack.py`), and reuses ones already running. They sign in through the real Keycloak and use the demo tenant `acme-pharma`. They set its QC sampling to 0, and the cases they create stay in the local database. A failed run leaves traces in `apps/review_ui/test-results`; open one with `npx playwright show-trace <trace.zip>`.

**On Windows**, start them from the folder's real letter case, for example `cd "D:\Resume projects\Pharma_Safe\apps\review_ui"` if that is how Explorer shows it. Otherwise Node loads a module twice and Playwright reports "did not expect test.beforeAll() to be called here".

## Database roles

The API and workers connect as `pharmasafe_app_user`. It is a non-owner role, so row-level security always applies. Migrations and `seed` connect as the owner (`PHARMASAFE_MIGRATION_DATABASE_URL`). See ADR 0008.

## Troubleshooting

- **`up` times out on keycloak or langfuse.** On a slow first start, Keycloak runs a build step before it serves traffic. Run `make up` again; it picks up where it left off.
- **Port already in use.** Override the port in `.env` (for example `POSTGRES_PORT=5433`).
- **Changed `deploy/postgres/init/*` but nothing happened, or `pharmasafe_app_user` does not exist.** Init scripts only run on an empty volume. Run `make reset`, then `make up`, `make migrate` and `make seed`.
- **Changed the Keycloak realm file but nothing happened.** `--import-realm` skips realms that already exist. Run `make reset`, or delete the `pharmasafe` realm in the admin console and restart Keycloak.
- **DB tests are skipped.** Set `PHARMASAFE_TEST_ADMIN_DATABASE_URL` in `.env` (see `.env.example`) and make sure Postgres is up.
- **`.env` missing variable error from compose.** Compare `.env` with `.env.example` and add the missing key.
