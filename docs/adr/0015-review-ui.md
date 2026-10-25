# ADR 0015: Review UI, duplicate merge, tenant administration and end-to-end tests

- Status: Accepted
- Date: 2026-10-25

## Context

Phase 6 builds the review UI. The build prompt asks for:

- queues sorted by deadline risk;
- a case workspace with:
  - the source document with evidence highlighting, next to the editable E2B-structured form;
  - confidence badges;
  - a seriousness panel showing the LLM next to the rules;
  - duplicate comparison and merge;
  - a coding picker, follow-up questions and the narrative;
- an e-signature dialog;
- version history and an audit viewer;
- tenant admin screens;
- WCAG 2.1 AA.

Acceptance: Playwright tests for intake-to-approval, duplicate merge and follow-up versioning.

The user made four choices:

1. React Aria Components as the accessible component library.
2. A merge copies the picked values and closes the duplicate.
3. Playwright runs on the full stack with real Keycloak.
4. Admin covers tenant settings and audit, while users and roles stay in the identity provider.

## Decision

### Stack

- React 18 with TanStack Query, as before.
- **React Router 7.18**: version 8 needs React 19.
- **React Aria Components 1.22**: accessible behaviour (labels, keyboard support, focus management, ARIA roles), with no visual style. `src/styles.css` adds a small token-based style. Its colours meet AA contrast, focus rings are 3:1 or more, and status always carries text, never colour alone.
- A plain HTML table where grid behaviour gets in the way: the merge comparison uses native checkboxes, because a React Aria grid row takes the pointer events of controls inside it.

### Screens

**Work queue.** Open tasks for the user's roles and data scope, nearest regulatory due date first. Each row shows a deadline-risk badge (overdue, ≤2 days, ≤5 days) and the AI run's flags: serious, duplicate, low confidence, AI unavailable. `GET /tasks` now returns each task's case stage and AI flags.

**Case workspace**, laid out in two columns:

- *Source document* (`GET /cases/{id}/source`). Every evidence span is highlighted. "Show evidence" on a field marks its span and scrolls to it. Offsets are document offsets, exactly as stored.
- *E2B form*, in the sections of the case model (`model.ts`). Every field shows:
  - its source: AI, person or import;
  - the AI's confidence, with values under 0.7 flagged as low;
  - whether it is confirmed.

  People confirm a value or replace it with their own. "Confirm all in this section" confirms only the values shown in that section, and approval still needs every value confirmed.
- *Seriousness*: each ICH E2A criterion is assessed per reaction as Yes, No or Not assessed, next to the LLM and rule signals and a "Signals disagree" badge.
- *MedDRA coding picker*: a combo box over `GET /coding/{kind}/search` (the dictionary adapter, now in `pharmasafe_api.coding`). Codes are picked, never typed.
- *AI panels*:
  - the automation level and its reasons;
  - suggestions at L0, which are not written to the case;
  - rejected values with reasons;
  - follow-up questions;
  - the narrative draft, made from confirmed values only, which can be used as a starting point for the narrative.
- *Task bar*: claim, sign and complete the step, or return the case to data entry with a reason.
- *History tab*: versions, with approved versions shown read-only, a follow-up start (receipt date), and the case's audit trail. `GET /cases/{id}/audit` is readable by anyone who can see the case.

**E-signature dialog.** It states what is signed and with which meaning, and takes a reason. Signing re-authenticates in a Keycloak popup (`prompt=login`, `max_age=0`), and the fresh token goes to the API as the re-authentication. The API binds the signature to the version's content hash and accepts each token once. A cancelled popup signs nothing.

**Tenant administration** (`/admin`, tenant admin role). The settings are:

- AI automation level;
- QC sampling rate;
- monthly AI budget;
- regulators whose clock rules apply;
- intake mailbox.

Each change is one audit entry per field, with the old and the new value. The screen also verifies the audit chain and lists the roles. Users, roles and data scopes stay in the identity provider (SCIM is later).

### Duplicate merge (`pharmasafe_api.cases.merge`)

`POST /cases/{duplicate}/merge` does all of this in one transaction:

1. Copies the picked fields, or whole items, into the master. A master with an approved latest version gets a follow-up version first.
2. Marks the copies `source: import`, unconfirmed, keeping their original evidence (the duplicate's document).
3. E-signs `Duplicate merged` on the duplicate's version.
4. Sets `cases.merged_into` permanently. A trigger refuses any change to it.
5. Cancels the duplicate's open tasks.
6. Sends `case.merged`, which ends the duplicate's workflow (stage `merged`). No escalation of its deadlines follows.

Approved duplicates cannot be merged, because they may have been submitted; nullification is a submission (Phase 7).

### Tenant self-administration (migration 0006)

- Row-level security on `tenants` lets every tenant read the directory and update only its own row, and column grants limit updates to the five settings. The owner role used by migrations and seeding is not subject to it.
- `ai_tenant_budgets` is writable through the app role, under RLS.
- The default budget is a shared setting (`ai_default_monthly_budget_usd`), used by both the API and the gateway.

### End-to-end tests

- **Specs.** Playwright (`apps/review_ui/e2e`) runs four specs:
  - intake to approval;
  - duplicate merge;
  - follow-up versioning;
  - axe WCAG 2.1 A/AA checks on the intake form, queue, workspace, history and admin screens.
- **Stack.** `scripts/e2e_stack.py` migrates, seeds and starts the API, the gateway (MockLLM bundle) and both workers. Playwright starts it and the UI dev server, and stops them afterwards.
- **Users.** Each person is a separate browser context signed in through real Keycloak. E-signatures go through the re-authentication popup.
- **CI.** A separate job brings up Postgres, Temporal, Keycloak and Azurite with docker-compose and runs the suite. The report and traces are uploaded on failure.

## Consequences

- **Bug fix.** Sign-in now processes the OIDC callback once per page load. React StrictMode runs effects twice in development, and the second code exchange failed ("Code not valid"). The bug dates from Phase 1 and was found by the end-to-end tests.
- **Confirmer names.** Confirmed values show "Confirmed by you" or "Confirmed". Values record the confirmer's subject id, and names are in the audit trail. Showing names on every field would need a user directory (SCIM, later).
- **Automated accessibility only.** axe finds many WCAG failures but not all. A manual keyboard and screen-reader review belongs in the validation pack (Phase 10).
- **Polling.** Queues and cases refresh by polling every 5 seconds. Server push is a later optimisation.
- **Not built yet:** L2 bulk QC sign-off (the flag is shown) and a WHODrug coding picker (the case model has no WHODrug field yet, Phase 7).
- **Windows quirk.** On Windows, run Playwright from the folder's real letter case: Node can otherwise load a module twice through differently cased paths.
