# ADR 0013: Durable case workflow, human tasks and deadline escalation

- Status: Accepted
- Date: 2026-10-25

## Context

Phase 4 adds the case workflow: received → pre-processing → AI processing → triage → data entry review → quality/medical review → approved → submitted → follow-up. It needs human tasks, assignment, the four-eyes rule, escalation timers before deadlines, and workflow versioning.

Acceptance: killing workers mid-case loses nothing and does not reset clocks; deadline escalations are tested with Temporal's time-skipping test environment.

## Decision

**One Temporal workflow per case,** with id `case-{case_id}` on the `case-workflow` task queue (`apps/workflow_worker`). It owns only two things: which human task is open next, and when to escalate.

**The rules live in the case store, not the workflow.** Each review step is an e-signature bound to the version's content hash (`pharmasafe_domain.rules.review`):

| Step | Signature meaning | Required for |
|---|---|---|
| Data entry review | `Reviewed` | every case |
| Quality review | `Quality reviewed` | serious cases, and a deterministic sample of non-serious cases (`tenants.qc_sample_rate`, default 10%) |
| Medical review | `Medically approved` | serious cases |
| Approval | `Approved` (locks the version) | every case |

- *Four eyes:* the data entry reviewer cannot sign any later step of the same version.
- *Order:* later steps need the data entry review first.
- *Edits void reviews:* only signatures on the current content hash count.

`cases.sign_review` and `cases.approve_version` check these rules from the signature records on every signature. A lost signal, a replayed history or a bug in the workflow can open the wrong task, but it can never approve a case that people have not reviewed properly.

**Human tasks** are rows in `review_tasks` (migration 0004), under row-level security:

- *Opening a task:* the workflow opens it through the `open_task` activity, with a task id it chose with `workflow.uuid4()`. A retry reuses the same id, so a retried activity never creates a second task. Opening a task cancels any other open task for the case.
- *Working a task:* people use `/tasks`:
  - `GET /tasks` is the work queue, filtered by role and data scope and sorted by the nearest regulatory due date;
  - `claim`, `assign`, `complete` and `return` act on one task.

  `complete` needs a fresh re-authentication token and applies the step's e-signature. `return` sends the case back to data entry with a reason.
- *Records:* a trigger keeps tasks immutable once closed and never deleted. At most one task of each kind is open per case. Every action is audited and announced through the outbox.

**Events reach the workflow through the outbox (ADR 0011).** The relay runs in the worker process and turns each `cases`-topic event into a *signal-with-start*, carrying the outbox event id. The first event for a case starts its workflow, and the workflow ignores ids it has already seen, because delivery is at least once. The workflow only records events in its state, and each wait is a condition on that state.

**Lifecycle details.**

- *Pre-processing:* this stage is recorded but done at intake, which is synchronous (ADR 0012).
- *AI processing:* a placeholder activity that returns automation level L0 until Phase 5.
- *Triage:* re-reads the latest version, its validity, its review plan and its deadlines. An invalid case waits in `held_for_follow_up` until it is edited.
- *Restarts:* an edit or a return during quality, medical review or approval restarts review from data entry, because the edit voided the signatures.
- *Follow-up:* when follow-up starts, the workflow *continues as new* for the new version, so a long-lived case does not grow an unbounded history.
- *Stage mirror:* `cases.workflow_stage` mirrors the current stage for queues and screens.

**Escalation timers.** For each regulatory clock of each unsubmitted version, the workflow raises three levels:

- `warning`, 5 days before the due date;
- `escalation`, 2 days before;
- `overdue`, the day after.

The offsets are per-workflow input. Each wait in the workflow is a Temporal timer set to the next escalation. Escalations are unique per (case, version, regulator, rule, level) in `deadline_escalations`, are audited, and go to the `notifications` topic. Escalation never moves the case or changes a clock.

**Durability.** Waits use Temporal timers and conditions, which survive worker crashes, restarts and deploys. Every activity is idempotent and runs in its own tenant-scoped transaction. Regulatory clocks stay rows in `case_clocks` that can never change (ADR 0010). The workflow reads them and never computes them.

**Versioning.**

- `WORKFLOW_VERSION` names the definition.
- An incompatible change to running workflows must be guarded with `workflow.patched("<change-id>")`.
- `tests/histories/*.json` holds recorded histories, and a replay test runs every one of them against the current code in CI. After a deliberate, patched change, set `PHARMASAFE_RECORD_HISTORIES=1` to record a new history and commit it.

## Consequences

- Workflow tests run on Temporal's time-skipping test server. The SDK downloads the server on first use, so CI needs network access to the Temporal CDN. Tests run with the workflow cache off when a worker is "killed", because the test server does not time out a dead worker's sticky queue the way a real cluster does.
- Submission (Phase 7) must emit `case.submitted` with the version, which stops that version's escalations and moves the workflow to `submitted`.
- Notifications for escalations (email, Teams) are not delivered yet. The events wait on the `notifications` topic for the Service Bus publisher (ADR 0011).
- Escalation offsets are the same for every regulator and rule. Per-rule offsets need a decision with regulatory affairs.
- Running pre-processing as workflow activities (planned in ADR 0012) was deferred. Intake stays synchronous and idempotent, which already meets the Phase 3 acceptance. It should move when OCR and AI processing become slow enough to matter (Phase 5).
