"""Activities of the case workflow: the database work it asks for.

Each activity is safe to run more than once, because Temporal retries activities after
failures and after a worker dies mid-activity. Each runs in its own tenant-scoped
transaction (row-level security applies) through the case and review modules' service
interfaces; none touches another module's tables directly.

Timestamps come from the workflow (``at``), so the record shows workflow time, which is
also what makes time-skipping tests meaningful.
"""

from collections.abc import Callable
from datetime import date, datetime
from typing import Any
from uuid import UUID

from temporalio import activity

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.review import service as review
from pharmasafe_domain.rules.validity import assess_validity
from pharmasafe_platform.db import Database
from pharmasafe_workflow_worker.contracts import (
    CaseRef,
    DeadlineInfo,
    EscalationRequest,
    OpenTaskRequest,
    StageRequest,
    TriageResult,
)

KIND_OF_STEP = {step.value: kind.value for step, kind in review.KIND_OF.items()}

# Automation level until the AI pipeline exists (Phase 5): L0, every field entered by people.
MANUAL_AUTOMATION_LEVEL = "L0"


class CaseActivities:
    def __init__(self, database: Database) -> None:
        self._database = database

    @activity.defn(name="set_stage")
    def set_stage(self, request: StageRequest) -> None:
        with self._database.tenant_session(UUID(request.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(request.case_id))
            review.set_stage(session, case, request.stage, datetime.fromisoformat(request.at))

    @activity.defn(name="ai_processing")
    def ai_processing(self, ref: CaseRef) -> str:
        """Placeholder for the AI pipeline (Phase 5): runs the case at L0 (manual)."""
        return MANUAL_AUTOMATION_LEVEL

    @activity.defn(name="triage")
    def triage(self, ref: CaseRef) -> TriageResult:
        with self._database.tenant_session(UUID(ref.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(ref.case_id))
            version = cases.get_version(session, case.id, case.latest_version)
            content = cases.case_of(version)
            plan = cases.review_plan_of(session, case, version)
            return TriageResult(
                version=version.version,
                approved=version.status == cases.APPROVED,
                valid=assess_validity(content).valid,
                steps=[KIND_OF_STEP[step.value] for step in plan.steps],
                deadlines=_deadlines(review.deadlines(session, case.id)),
            )

    @activity.defn(name="deadlines")
    def deadlines(self, ref: CaseRef) -> list[DeadlineInfo]:
        with self._database.tenant_session(UUID(ref.tenant_id)) as session:
            return _deadlines(review.deadlines(session, UUID(ref.case_id)))

    @activity.defn(name="open_task")
    def open_task(self, request: OpenTaskRequest) -> None:
        with self._database.tenant_session(UUID(request.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(request.case_id))
            review.open_task(
                session,
                case,
                request.version,
                review.Kind(request.kind),
                UUID(request.task_id),
                datetime.fromisoformat(request.at),
            )

    @activity.defn(name="escalate")
    def escalate(self, request: EscalationRequest) -> bool:
        d = request.deadline
        deadline = review.Deadline(
            d.regulator, d.rule_id, d.case_version, date.fromisoformat(d.due_date)
        )
        with self._database.tenant_session(UUID(request.tenant_id)) as session:
            case = session.get_one(CaseRow, UUID(request.case_id))
            return review.record_escalation(
                session, case, deadline, request.level, datetime.fromisoformat(request.at)
            )

    def all(self) -> list[Callable[..., Any]]:
        return [
            self.set_stage,
            self.ai_processing,
            self.triage,
            self.deadlines,
            self.open_task,
            self.escalate,
        ]


def _deadlines(rows: list[review.Deadline]) -> list[DeadlineInfo]:
    return [
        DeadlineInfo(d.regulator, d.rule_id, d.case_version, d.due_date.isoformat()) for d in rows
    ]
