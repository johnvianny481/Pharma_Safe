"""The durable case workflow (Temporal). One workflow per case: ``case-{case_id}``.

Lifecycle::

    received -> pre_processing -> ai_processing -> triage
      -> (invalid) held_for_follow_up -> back to triage when the case changes
      -> data_entry_review -> quality_medical_review (serious or QC-sampled) -> approval
      -> approved -> submitted -> follow_up -> triage of the new version ...

**What the workflow owns:** which human task is open next, and when to escalate.
**What it does not own:** the rules. Whether a signature may be applied, and whether a
version may be approved, is enforced by the case store from the signature records
(four-eyes, review order). Regulatory clocks are rows in ``case_clocks`` that can never
change. A crashed worker, a replayed history or a lost signal cannot break either.

**Events.** Outbox events (``case.updated``, ``task.completed``, ...) arrive as the
``case_event`` signal, at least once. Each event only updates the workflow's state
(ignoring ids already seen); each wait is a condition on that state.

**Durability.** Waiting for people and for deadlines uses Temporal timers and
conditions, which survive worker restarts and deploys. Activities are idempotent: a
retried ``open_task`` reuses the task id the workflow chose with ``workflow.uuid4()``,
and escalations are unique per deadline and level.

**AI processing.** The AI pipeline runs as a child workflow on the ``ai-pipeline`` task
queue (``pharmasafe_ai_worker``). It always finishes: if the AI is unavailable it records
the case in L0 manual mode, and if the child itself fails, the case still goes on to
triage. After data entry, a narrative draft is requested from confirmed fields; it is a
convenience and never blocks review.

**Versioning.** ``WORKFLOW_VERSION`` identifies this definition. Incompatible changes to
running workflows must be guarded with ``workflow.patched("<change-id>")``, and the
replay test (``tests/histories``) must keep passing against recorded histories.

- v1 (Phase 4): AI processing was a placeholder activity.
- v2 (Phase 5): patches ``ai-pipeline`` (the child workflow) and ``narrative-draft``.
"""

import contextlib
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ChildWorkflowError

with workflow.unsafe.imports_passed_through():
    from pharmasafe_ai_worker.contracts import (
        PIPELINE_WORKFLOW,
        NarrativeInput,
        PipelineInput,
        pipeline_workflow_id,
    )
    from pharmasafe_ai_worker.contracts import TASK_QUEUE as AI_TASK_QUEUE
    from pharmasafe_workflow_worker.contracts import (
        SIGNAL,
        WORKFLOW_NAME,
        CaseEvent,
        CaseRef,
        CaseWorkflowInput,
        DeadlineInfo,
        EscalationRequest,
        OpenTaskRequest,
        StageRequest,
        TriageResult,
        WorkflowState,
    )

WORKFLOW_VERSION = 2
AI_PIPELINE_PATCH = "ai-pipeline"
NARRATIVE_PATCH = "narrative-draft"

ACTIVITY_TIMEOUT = timedelta(minutes=2)
AI_TIMEOUT = timedelta(minutes=30)
NARRATIVE_TIMEOUT = timedelta(minutes=10)
NARRATIVE_RETRY = RetryPolicy(initial_interval=timedelta(seconds=2), maximum_attempts=3)
RETRY = RetryPolicy(initial_interval=timedelta(seconds=1), maximum_interval=timedelta(minutes=1))
STAGE_OF_TASK = {
    "data_entry": "data_entry_review",
    "quality_review": "quality_medical_review",
    "medical_review": "quality_medical_review",
    "approval": "approval",
}
LEVELS = ("warning", "escalation", "overdue")

Escalation = tuple[datetime, DeadlineInfo, str]


@workflow.defn(name=WORKFLOW_NAME)
class CaseWorkflow:
    def __init__(self) -> None:
        self._inbox: list[CaseEvent] = []
        self._seen: set[str] = set()
        self._input: CaseWorkflowInput | None = None
        self._stage = "received"
        self._version = 1
        self._deadlines: list[DeadlineInfo] = []
        self._escalated: set[tuple[str, str, int, str]] = set()
        # State built from events.
        self._task_outcomes: dict[str, str] = {}
        self._approved_versions: set[int] = set()
        self._updates = 0
        self._follow_up = False
        self._submitted_version = 0
        self._deadlines_stale = False

    # ------------------------------------------------------------ interface

    @workflow.signal(name=SIGNAL)
    def case_event(self, event: CaseEvent) -> None:
        if event.event_id not in self._seen:
            self._seen.add(event.event_id)
            self._inbox.append(event)

    @workflow.query
    def state(self) -> WorkflowState:
        return WorkflowState(WORKFLOW_VERSION, self._stage, self._version)

    # ------------------------------------------------------------ the lifecycle

    @workflow.run
    async def run(self, inp: CaseWorkflowInput) -> None:
        self._input = inp
        if not inp.resume:
            await self._set_stage("received")
            await self._set_stage("pre_processing")  # done at intake (ADR 0012)
            await self._set_stage("ai_processing")
            if workflow.patched(AI_PIPELINE_PATCH):
                await self._ai_pipeline()
            else:  # workflows started before Phase 5 keep the L0 placeholder
                await self._activity("ai_processing", self._ref(), str)

        while True:
            triage = await self._triage()
            if triage.approved:
                break
            if not triage.valid:
                await self._set_stage("held_for_follow_up")
                await self._wait_for_new_information()
                continue
            if await self._review(triage) == "approved":
                break

        await self._set_stage("approved")
        await self._wait_until(lambda: self._submitted_version >= self._version or self._follow_up)
        if self._submitted_version >= self._version:
            await self._set_stage("submitted")
            await self._wait_until(lambda: self._follow_up)
        await self._set_stage("follow_up")
        # The new version starts a fresh history; clocks and tasks live in the database.
        workflow.continue_as_new(
            CaseWorkflowInput(
                inp.tenant_id, inp.case_id, inp.warn_days, inp.escalate_days, resume=True
            )
        )

    async def _wait_for_new_information(self) -> None:
        before = self._updates
        await self._wait_until(lambda: self._updates > before or self._follow_up)

    async def _review(self, first: TriageResult) -> str:
        """Run the review steps for one version. Returns "approved" or "restart"."""
        outcome = await self._task("data_entry", first.version)
        if outcome != "completed":
            return outcome
        if workflow.patched(NARRATIVE_PATCH):
            await self._draft_narrative(first.version)
        # Data entry may have changed seriousness, so plan the remaining steps now.
        plan = await self._triage()
        if plan.approved:
            return "approved"
        if not plan.valid:
            return "restart"
        for kind in plan.steps[1:]:
            outcome = await self._task(kind, plan.version)
            if outcome != "completed":
                return outcome
        return "approved"

    async def _task(self, kind: str, version: int) -> str:
        """Open a task and wait for it. Returns "completed", "approved" or "restart"."""
        inp = self._require_input()
        task_id = str(workflow.uuid4())
        await self._set_stage(STAGE_OF_TASK[kind])
        request = OpenTaskRequest(inp.tenant_id, inp.case_id, version, kind, task_id, self._now())
        await self._activity("open_task", request, None)
        updates_before = self._updates

        def edited_after_data_entry() -> bool:
            # Edits void the review signatures, so review starts again from data entry.
            return kind != "data_entry" and self._updates > updates_before

        await self._wait_until(
            lambda: (
                task_id in self._task_outcomes
                or version in self._approved_versions
                or edited_after_data_entry()
            )
        )
        if version in self._approved_versions:
            return "approved"
        if self._task_outcomes.get(task_id) == "task.completed":
            return "completed"
        return "restart"

    # ------------------------------------------------------------ events, waiting, escalation

    def _absorb(self, event: CaseEvent) -> None:
        if event.type in ("task.completed", "task.returned") and event.task_id:
            self._task_outcomes[event.task_id] = event.type
        elif event.type == "case.version_approved" and event.version is not None:
            self._approved_versions.add(event.version)
        elif event.type == "case.updated":
            self._updates += 1
        elif event.type == "case.follow_up_started":
            self._follow_up = True
        elif event.type == "case.submitted":
            self._submitted_version = max(self._submitted_version, event.version or self._version)
        elif event.type == "case.clock_started":
            self._deadlines_stale = True

    async def _wait_until(self, done: Callable[[], bool]) -> None:
        """Wait until ``done()`` holds, applying events and escalating deadlines meanwhile."""
        while True:
            while self._inbox:
                self._absorb(self._inbox.pop(0))
            if self._deadlines_stale:
                self._deadlines_stale = False
                await self._refresh_deadlines()
            if done():
                return
            nxt = self._next_escalation()
            if nxt is not None and nxt[0] <= workflow.now():
                await self._escalate(nxt)
                continue
            timeout = (nxt[0] - workflow.now()) if nxt is not None else None
            with contextlib.suppress(TimeoutError):  # a deadline is due: loop and escalate
                await workflow.wait_condition(lambda: bool(self._inbox), timeout=timeout)

    def _next_escalation(self) -> Escalation | None:
        """The earliest escalation not yet raised for a deadline not yet submitted."""
        inp = self._require_input()
        offset_days = {"warning": -inp.warn_days, "escalation": -inp.escalate_days, "overdue": 1}
        best: Escalation | None = None
        for deadline in self._deadlines:
            if deadline.case_version <= self._submitted_version:
                continue
            due = date.fromisoformat(deadline.due_date)
            midnight = datetime(due.year, due.month, due.day, tzinfo=UTC)
            for level in LEVELS:
                key = (deadline.regulator, deadline.rule_id, deadline.case_version, level)
                at = midnight + timedelta(days=offset_days[level])
                if key not in self._escalated and (best is None or at < best[0]):
                    best = (at, deadline, level)
        return best

    async def _escalate(self, escalation: Escalation) -> None:
        inp = self._require_input()
        _, deadline, level = escalation
        request = EscalationRequest(inp.tenant_id, inp.case_id, deadline, level, self._now())
        await self._activity("escalate", request, bool)
        self._escalated.add((deadline.regulator, deadline.rule_id, deadline.case_version, level))

    # ------------------------------------------------------------ activities

    async def _triage(self) -> TriageResult:
        await self._set_stage("triage")
        result = await self._activity("triage", self._ref(), TriageResult)
        self._version = result.version
        self._deadlines = result.deadlines
        return result

    async def _ai_pipeline(self) -> None:
        inp = self._require_input()
        try:
            await workflow.execute_child_workflow(
                PIPELINE_WORKFLOW,
                PipelineInput(inp.tenant_id, inp.case_id, self._version),
                id=pipeline_workflow_id(inp.case_id, self._version),
                task_queue=AI_TASK_QUEUE,
                execution_timeout=AI_TIMEOUT,
            )
        except ChildWorkflowError:
            # The pipeline records L0 itself when the AI is down; reaching here means the
            # pipeline could not even finish. People take over, as at L0.
            workflow.logger.warning("AI pipeline failed; continuing with manual processing")

    async def _draft_narrative(self, version: int) -> None:
        inp = self._require_input()
        try:
            await workflow.execute_activity(
                "draft_narrative",
                NarrativeInput(inp.tenant_id, inp.case_id, version),
                result_type=str,
                task_queue=AI_TASK_QUEUE,
                start_to_close_timeout=NARRATIVE_TIMEOUT,
                retry_policy=NARRATIVE_RETRY,
            )
        except ActivityError:
            workflow.logger.warning("narrative draft failed; reviewers write it by hand")

    async def _refresh_deadlines(self) -> None:
        self._deadlines = await self._activity("deadlines", self._ref(), list[DeadlineInfo])

    async def _set_stage(self, stage: str) -> None:
        inp = self._require_input()
        self._stage = stage
        await self._activity(
            "set_stage", StageRequest(inp.tenant_id, inp.case_id, stage, self._now()), None
        )

    async def _activity[T](self, name: str, arg: object, result_type: type[T] | None) -> T:
        result: T = await workflow.execute_activity(
            name,
            arg,
            result_type=result_type,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=RETRY,
        )
        return result

    def _ref(self) -> CaseRef:
        inp = self._require_input()
        return CaseRef(inp.tenant_id, inp.case_id)

    def _now(self) -> str:
        return workflow.now().isoformat()

    def _require_input(self) -> CaseWorkflowInput:
        if self._input is None:
            raise RuntimeError("workflow input is not set")
        return self._input
