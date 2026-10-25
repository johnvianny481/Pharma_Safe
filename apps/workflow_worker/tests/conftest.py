"""Fixtures for case workflow tests: a Temporal time-skipping server, the real database,
and a small driver that plays the outbox relay and the people working the tasks."""

import asyncio
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import select, text
from temporalio.client import Client, WorkflowHandle
from temporalio.worker import Worker

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseClockRow, CaseRow
from pharmasafe_api.review import service as review
from pharmasafe_api.review.models import DeadlineEscalationRow, ReviewTaskRow
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.changes import SetField
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.auth import Principal
from pharmasafe_platform.db import Database
from pharmasafe_platform.outbox import OutboxRelay
from pharmasafe_platform.signatures import SignatureService
from pharmasafe_workflow_worker.activities import CaseActivities
from pharmasafe_workflow_worker.contracts import TASK_QUEUE, workflow_id
from pharmasafe_workflow_worker.publisher import TemporalPublisher
from pharmasafe_workflow_worker.workflows import CaseWorkflow

REGISTRY = seed_registry()
SIGNATURES = SignatureService(max_auth_age=timedelta(minutes=5))
CRITERIA = (
    "death",
    "life_threatening",
    "hospitalisation",
    "disability",
    "congenital_anomaly",
    "other_medically_important",
)
TASK_KINDS_OF_STAGE = {
    "data_entry_review": {"data_entry"},
    "quality_medical_review": {"quality_review", "medical_review"},
    "approval": {"approval"},
}
PEOPLE = {
    "data_entry": ("proc-1", "case_processor"),
    "quality_review": ("qa-2", "quality_reviewer"),
    "medical_review": ("doc-1", "medical_reviewer"),
    "approval": ("qa-1", "quality_reviewer"),
}


def case_content(receipt: date, *, serious: bool, valid: bool = True) -> SafetyCase:
    def h(value: Any) -> dict[str, Any]:
        return Tracked[Any].human(value, by="proc-0", at=datetime.now(UTC)).model_dump(mode="json")

    return SafetyCase.from_json(
        {
            "admin": {
                "report_type": h("spontaneous"),
                "initial_receipt_date": h(receipt.isoformat()),
                "latest_receipt_date": h(receipt.isoformat()),
                "occurrence_country": h("US"),
            },
            "patient": {"initials": h("JD")} if valid else {},
            "reporters": [{"id": "p1", "qualification": h("physician")}],
            "drugs": [{"id": "d1", "role": h("suspect"), "product_name": h("Acmezol")}],
            "reactions": [
                {
                    "id": "r1",
                    "reported_term": h("rash"),
                    "expectedness": h("unexpected"),
                    "seriousness": {c: h(serious and c == "hospitalisation") for c in CRITERIA},
                }
            ],
        }
    )


@dataclass
class Driver:
    """Plays the outbox relay and the people working tasks, against a real database."""

    client: Client
    db: Database
    tenant: UUID
    make_principal: Callable[..., Principal]
    executor: ThreadPoolExecutor

    def worker(
        self, activities: list[Callable[..., Any]] | None = None, *, cache: bool = True
    ) -> Worker:
        return Worker(
            self.client,
            task_queue=TASK_QUEUE,
            workflows=[CaseWorkflow],
            activities=activities if activities is not None else CaseActivities(self.db).all(),
            activity_executor=self.executor,
            max_cached_workflows=1000 if cache else 0,
        )

    def person(self, sub: str, role: str) -> Principal:
        return self.make_principal(sub=sub, tenant_id=str(self.tenant), roles=[role])

    def create_case(self, content: SafetyCase) -> UUID:
        with self.db.tenant_session(self.tenant) as session:
            case, _ = cases.create_case(
                session,
                self.tenant,
                Actor("proc-0", "Intake"),
                content,
                cases.CaseContext(REGISTRY, datetime.now(UTC)),
            )
            return case.id

    async def relay(self) -> None:
        """Deliver this tenant's committed outbox events to Temporal."""
        publisher = TemporalPublisher(self.client, asyncio.get_running_loop())
        await asyncio.to_thread(OutboxRelay(self.db, publisher).run_once, [self.tenant])

    def handle(self, case_id: UUID) -> WorkflowHandle[Any, Any]:
        return self.client.get_workflow_handle(workflow_id(str(case_id)))

    async def stage_becomes(self, case_id: UUID, stage: str, timeout: float = 30) -> None:
        """Wait for the stage, and for its task to be open (the workflow opens it just after)."""
        kinds = TASK_KINDS_OF_STAGE.get(stage)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.relay()
            if self.stage(case_id) == stage and (
                kinds is None or any(k in kinds and s == "open" for k, s in self.tasks(case_id))
            ):
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"case stayed in {self.stage(case_id)!r}, expected {stage!r}")

    async def task_becomes(self, case_id: UUID, kind: str, timeout: float = 30) -> ReviewTaskRow:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            await self.relay()
            with self.db.tenant_session(self.tenant) as session:
                open_tasks = review.open_tasks_of(session, case_id)
            if open_tasks and open_tasks[0].kind == kind:
                return open_tasks[0]
            await asyncio.sleep(0.2)
        raise AssertionError(f"no open {kind} task; tasks: {self.tasks(case_id)}")

    def stage(self, case_id: UUID) -> str:
        with self.db.tenant_session(self.tenant) as session:
            return session.get_one(CaseRow, case_id).workflow_stage

    def open_task(self, case_id: UUID) -> ReviewTaskRow:
        with self.db.tenant_session(self.tenant) as session:
            (task,) = review.open_tasks_of(session, case_id)
            return task

    def tasks(self, case_id: UUID) -> list[tuple[str, str]]:
        with self.db.tenant_session(self.tenant) as session:
            rows = session.scalars(
                select(ReviewTaskRow)
                .where(ReviewTaskRow.case_id == case_id)
                .order_by(ReviewTaskRow.created_at)
            )
            return [(t.kind, t.status) for t in rows]

    def complete(
        self, task: ReviewTaskRow, sub: str | None = None, role: str | None = None
    ) -> None:
        default_sub, default_role = PEOPLE[task.kind]
        person = self.person(sub or default_sub, role or default_role)
        with self.db.tenant_session(self.tenant) as session:
            review.complete(
                session,
                task.id,
                person,
                person,
                SIGNATURES,
                cases.CaseContext(REGISTRY, datetime.now(UTC)),
                "checked",
            )

    def return_task(self, task: ReviewTaskRow, reason: str) -> None:
        sub, role = PEOPLE[task.kind]
        with self.db.tenant_session(self.tenant) as session:
            review.return_task(session, task.id, self.person(sub, role), reason, datetime.now(UTC))

    def edit(self, case_id: UUID, path: str, value: Any) -> None:
        person = self.person("proc-1", "case_processor")
        tracked = (
            Tracked[Any].human(value, by="proc-1", at=datetime.now(UTC)).model_dump(mode="json")
        )
        with self.db.tenant_session(self.tenant) as session:
            case = session.get_one(CaseRow, case_id)
            version = cases.get_version(session, case_id, case.latest_version)
            cases.apply_changes(
                session,
                case,
                version.version,
                expected_revision=version.revision,
                changes=[SetField(path=path, value=tracked)],
                principal=person,
                ctx=cases.CaseContext(REGISTRY, datetime.now(UTC)),
            )

    def follow_up(self, case_id: UUID, receipt: date) -> None:
        with self.db.tenant_session(self.tenant) as session:
            case = session.get_one(CaseRow, case_id)
            cases.start_follow_up(
                session,
                case,
                receipt_date=receipt,
                principal=self.person("proc-1", "case_processor"),
                ctx=cases.CaseContext(REGISTRY, datetime.now(UTC)),
            )

    def clocks(self, case_id: UUID) -> list[tuple[Any, ...]]:
        with self.db.tenant_session(self.tenant) as session:
            rows = session.scalars(
                select(CaseClockRow)
                .where(CaseClockRow.case_id == case_id)
                .order_by(CaseClockRow.id)
            )
            return [
                (c.regulator, c.rule_id, c.case_version, c.clock_start, c.due_date) for c in rows
            ]

    def escalations(self, case_id: UUID) -> list[tuple[str, str, str]]:
        with self.db.tenant_session(self.tenant) as session:
            rows = session.scalars(
                select(DeadlineEscalationRow)
                .where(DeadlineEscalationRow.case_id == case_id)
                .order_by(DeadlineEscalationRow.id)
            )
            return [(e.regulator, e.rule_id, e.level) for e in rows]

    async def escalations_reach(self, case_id: UUID, count: int, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if len(self.escalations(case_id)) >= count:
                return
            await asyncio.sleep(0.2)
        raise AssertionError(f"escalations: {self.escalations(case_id)}")


@pytest.fixture(scope="session")
def make_content() -> Callable[..., SafetyCase]:
    return case_content


@pytest.fixture
def no_qc_sampling(owner_engine: Any, tenant_a: UUID) -> None:
    """Non-serious cases go straight from data entry to approval."""
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE tenants SET qc_sample_rate = 0 WHERE id = :t"), {"t": tenant_a})


@pytest.fixture
def driver_factory(
    db: Database, tenant_a: UUID, make_principal: Callable[..., Principal]
) -> Callable[[Client, ThreadPoolExecutor], Driver]:
    def build(client: Client, executor: ThreadPoolExecutor) -> Driver:
        return Driver(client, db, tenant_a, make_principal, executor)

    return build
