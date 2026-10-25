"""Human review tasks: opened by the case workflow, worked by people.

The workflow (Phase 4, Temporal) decides *which* task comes next and opens it here.
People claim, complete or return tasks through the API. Completing a task applies the
step's e-signature through the case store, which enforces the review order and the
four-eyes rule (:mod:`pharmasafe_domain.rules.review`). The workflow then hears about it
through the outbox (``task.completed`` / ``task.returned``) and moves on.

Every function is idempotent where the workflow calls it, because Temporal retries
activities: a task is identified by an id the workflow chose, and opening it twice
returns the first one.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseAiRunRow, CaseClockRow, CaseRow
from pharmasafe_api.review.models import DeadlineEscalationRow, ReviewTaskRow
from pharmasafe_domain.rules.review import Step
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.auth import AuthorizationError, Principal, Role
from pharmasafe_platform.outbox import OutboxEvent, enqueue
from pharmasafe_platform.signatures import SignatureService

ENTITY = "review_task"
SYSTEM_ACTOR = Actor(id="system:workflow", name="Case workflow")


class Kind(StrEnum):
    DATA_ENTRY = "data_entry"
    QUALITY_REVIEW = "quality_review"
    MEDICAL_REVIEW = "medical_review"
    APPROVAL = "approval"


class Status(StrEnum):
    OPEN = "open"
    COMPLETED = "completed"
    RETURNED = "returned"
    CANCELLED = "cancelled"


STEP_OF: Mapping[Kind, Step] = {
    Kind.DATA_ENTRY: Step.DATA_ENTRY,
    Kind.QUALITY_REVIEW: Step.QUALITY,
    Kind.MEDICAL_REVIEW: Step.MEDICAL,
    Kind.APPROVAL: Step.APPROVAL,
}
KIND_OF: Mapping[Step, Kind] = {step: kind for kind, step in STEP_OF.items()}
ROLES_FOR: Mapping[Kind, frozenset[Role]] = {
    Kind.DATA_ENTRY: frozenset({Role.CASE_PROCESSOR}),
    Kind.QUALITY_REVIEW: frozenset({Role.QUALITY_REVIEWER}),
    Kind.MEDICAL_REVIEW: frozenset({Role.MEDICAL_REVIEWER}),
    Kind.APPROVAL: frozenset({Role.QUALITY_REVIEWER, Role.MEDICAL_REVIEWER}),
}


class TaskNotFound(Exception):
    pass


class TaskConflict(Exception):
    """The task is not open, or belongs to someone else."""


# ---------------------------------------------------------------- called by the workflow


def open_task(
    session: Session, case: CaseRow, version: int, kind: Kind, task_id: UUID, now: datetime
) -> ReviewTaskRow:
    """Open a task (idempotent on ``task_id``). Any other open task of the case is cancelled."""
    existing = session.get(ReviewTaskRow, task_id)
    if existing is not None:
        return existing
    cancel_open_tasks(session, case, now, reason=f"superseded by {kind.value}")
    session.execute(
        insert(ReviewTaskRow)
        .values(
            id=task_id,
            tenant_id=case.tenant_id,
            case_id=case.id,
            case_version=version,
            kind=kind.value,
            status=Status.OPEN.value,
            created_at=now,
        )
        .on_conflict_do_nothing()
    )
    task = session.get_one(ReviewTaskRow, task_id)
    _audit(
        session, task, SYSTEM_ACTOR, now, "task.opened", {"kind": kind.value, "version": version}
    )
    return task


def cancel_open_tasks(session: Session, case: CaseRow, now: datetime, *, reason: str) -> None:
    open_tasks = session.scalars(
        select(ReviewTaskRow)
        .where(ReviewTaskRow.case_id == case.id, ReviewTaskRow.status == Status.OPEN.value)
        .with_for_update()
    ).all()
    for task in open_tasks:
        _close(task, Status.CANCELLED, SYSTEM_ACTOR, now, reason)
        _audit(session, task, SYSTEM_ACTOR, now, "task.cancelled", {"reason": reason})
    session.flush()


def set_stage(session: Session, case: CaseRow, stage: str, now: datetime) -> None:
    if case.workflow_stage == stage:
        return
    old = case.workflow_stage
    case.workflow_stage = stage
    session.flush()
    append_audit(
        session,
        case.tenant_id,
        AuditEvent(
            actor=SYSTEM_ACTOR,
            action="case.stage_changed",
            entity_type="case",
            entity_id=str(case.id),
            case_version=case.latest_version,
            field="workflow_stage",
            old_value=old,
            new_value=stage,
        ),
        now=now,
    )


@dataclass(frozen=True)
class Deadline:
    regulator: str
    rule_id: str
    case_version: int
    due_date: date


def deadlines(session: Session, case_id: UUID) -> list[Deadline]:
    return [
        Deadline(c.regulator, c.rule_id, c.case_version, c.due_date)
        for c in cases.list_clocks(session, case_id)
    ]


def record_escalation(
    session: Session, case: CaseRow, deadline: Deadline, level: str, now: datetime
) -> bool:
    """Record one escalation level for one deadline, once. Returns whether it was new."""
    inserted = session.execute(
        insert(DeadlineEscalationRow)
        .values(
            tenant_id=case.tenant_id,
            case_id=case.id,
            case_version=deadline.case_version,
            regulator=deadline.regulator,
            rule_id=deadline.rule_id,
            due_date=deadline.due_date,
            level=level,
            created_at=now,
        )
        .on_conflict_do_nothing()
        .returning(DeadlineEscalationRow.id)
    ).scalar_one_or_none()
    if inserted is None:
        return False
    value = {
        "regulator": deadline.regulator,
        "rule_id": deadline.rule_id,
        "due_date": deadline.due_date.isoformat(),
        "level": level,
    }
    append_audit(
        session,
        case.tenant_id,
        AuditEvent(
            actor=SYSTEM_ACTOR,
            action="case.deadline_escalated",
            entity_type="case",
            entity_id=str(case.id),
            case_version=deadline.case_version,
            new_value=value,
        ),
        now=now,
    )
    enqueue(
        session,
        case.tenant_id,
        OutboxEvent(
            "notifications",
            "case.deadline_escalated",
            str(case.id),
            {"case_id": str(case.id), "case_number": case.case_number, **value},
        ),
        now=now,
    )
    return True


# ---------------------------------------------------------------- called by people


@dataclass(frozen=True)
class QueueItem:
    task: ReviewTaskRow
    case_number: str
    next_due: date | None
    stage: str
    # Flags of the AI run on this version (serious, duplicates, low confidence, ...).
    flags: list[str]


def queue(
    session: Session, principal: Principal, *, mine_only: bool = False, limit: int = 100
) -> list[QueueItem]:
    """Open tasks the caller can work, most urgent deadline first (no deadline last)."""
    kinds = [k.value for k, roles in ROLES_FOR.items() if principal.has_any_role(*roles)]
    if not kinds:
        return []
    next_due = (
        select(func.min(CaseClockRow.due_date))
        .where(CaseClockRow.case_id == ReviewTaskRow.case_id)
        .scalar_subquery()
    )
    flags = (
        select(CaseAiRunRow.flags)
        .where(
            CaseAiRunRow.case_id == ReviewTaskRow.case_id,
            CaseAiRunRow.case_version == ReviewTaskRow.case_version,
            CaseAiRunRow.kind == "pipeline",
        )
        .scalar_subquery()
    )
    query = (
        select(ReviewTaskRow, CaseRow.case_number, next_due, CaseRow.workflow_stage, flags)
        .join(CaseRow, CaseRow.id == ReviewTaskRow.case_id)
        .where(
            ReviewTaskRow.status == Status.OPEN.value,
            ReviewTaskRow.kind.in_(kinds),
            cases.scope_filter(principal.scope),
        )
        .order_by(next_due.asc().nulls_last(), ReviewTaskRow.created_at)
        .limit(limit)
    )
    if mine_only:
        query = query.where(ReviewTaskRow.assignee == principal.subject)
    else:
        query = query.where(
            (ReviewTaskRow.assignee.is_(None)) | (ReviewTaskRow.assignee == principal.subject)
        )
    return [
        QueueItem(task, number, due, stage, list(run_flags or []))
        for task, number, due, stage, run_flags in session.execute(query)
    ]


def get_task(
    session: Session, task_id: UUID, principal: Principal
) -> tuple[ReviewTaskRow, CaseRow]:
    task = session.get(ReviewTaskRow, task_id)
    if task is None:
        raise TaskNotFound(str(task_id))
    try:
        case = cases.get_case(session, task.case_id, principal.scope)
    except cases.CaseNotFound as exc:
        raise TaskNotFound(str(task_id)) from exc
    return task, case


def claim(session: Session, task_id: UUID, principal: Principal, now: datetime) -> ReviewTaskRow:
    task, _ = _open_task_for_update(session, task_id, principal)
    _require_role(task, principal)
    if task.assignee not in (None, principal.subject):
        raise TaskConflict("the task is assigned to someone else")
    if task.assignee is None:
        task.assignee = principal.subject
        session.flush()
        _audit(
            session,
            task,
            Actor.from_principal(principal),
            now,
            "task.claimed",
            {"assignee": principal.subject},
        )
    return task


def assign(
    session: Session, task_id: UUID, assignee: str, principal: Principal, now: datetime
) -> ReviewTaskRow:
    """Supervisors (tenant admins) assign work. The assignee's role is checked when they act."""
    if not principal.has_any_role(Role.TENANT_ADMIN):
        raise AuthorizationError("only a tenant admin can assign tasks")
    task, _ = _open_task_for_update(session, task_id, principal)
    old = task.assignee
    task.assignee = assignee
    session.flush()
    _audit(
        session,
        task,
        Actor.from_principal(principal),
        now,
        "task.assigned",
        {"assignee": assignee},
        old,
    )
    return task


def complete(
    session: Session,
    task_id: UUID,
    principal: Principal,
    reauth: Principal,
    signatures: SignatureService,
    ctx: cases.CaseContext,
    reason: str | None = None,
) -> ReviewTaskRow:
    """Sign the task's step on the current content and close the task."""
    task, case = _open_task_for_update(session, task_id, principal)
    _require_role(task, principal)
    if task.assignee not in (None, principal.subject):
        raise TaskConflict("the task is assigned to someone else")
    step = STEP_OF[Kind(task.kind)]
    if step is Step.APPROVAL:
        version = cases.approve_version(
            session,
            case,
            task.case_version,
            signer=principal,
            reauth=reauth,
            signatures=signatures,
            ctx=ctx,
            reason=reason,
        )
        signature_id = version.signature_id
    else:
        signature = cases.sign_review(
            session,
            case,
            task.case_version,
            step=step,
            signer=principal,
            reauth=reauth,
            signatures=signatures,
            ctx=ctx,
            reason=reason,
        )
        signature_id = signature.id
    actor = Actor.from_principal(principal)
    task.signature_id = signature_id
    _close(task, Status.COMPLETED, actor, ctx.now, reason)
    session.flush()
    _audit(session, task, actor, ctx.now, "task.completed", {"signature_id": str(signature_id)})
    _announce(session, task, case, "task.completed", ctx.now)
    return task


def return_task(
    session: Session, task_id: UUID, principal: Principal, reason: str, now: datetime
) -> ReviewTaskRow:
    """Send the case back to data entry, with the reason."""
    task, case = _open_task_for_update(session, task_id, principal)
    _require_role(task, principal)
    if task.kind == Kind.DATA_ENTRY.value:
        raise TaskConflict("a data entry task cannot be returned")
    actor = Actor.from_principal(principal)
    _close(task, Status.RETURNED, actor, now, reason)
    session.flush()
    _audit(session, task, actor, now, "task.returned", {"reason": reason})
    _announce(session, task, case, "task.returned", now)
    return task


# ---------------------------------------------------------------- internals


def _open_task_for_update(
    session: Session, task_id: UUID, principal: Principal
) -> tuple[ReviewTaskRow, CaseRow]:
    get_task(session, task_id, principal)  # scope check
    task = session.scalars(
        select(ReviewTaskRow).where(ReviewTaskRow.id == task_id).with_for_update()
    ).one()
    if task.status != Status.OPEN.value:
        raise TaskConflict(f"the task is {task.status}")
    return task, session.get_one(CaseRow, task.case_id)


def _require_role(task: ReviewTaskRow, principal: Principal) -> None:
    if not principal.has_any_role(*ROLES_FOR[Kind(task.kind)]):
        raise AuthorizationError(f"your roles cannot work a {task.kind} task")


def _close(
    task: ReviewTaskRow, status: Status, actor: Actor, now: datetime, reason: str | None
) -> None:
    task.status = status.value
    task.closed_at = now
    task.closed_by = actor.id
    task.close_reason = reason


def _announce(
    session: Session, task: ReviewTaskRow, case: CaseRow, event_type: str, now: datetime
) -> None:
    enqueue(
        session,
        task.tenant_id,
        OutboxEvent(
            "cases",
            event_type,
            str(case.id),
            {
                "case_id": str(case.id),
                "case_number": case.case_number,
                "task_id": str(task.id),
                "kind": task.kind,
                "version": task.case_version,
            },
        ),
        now=now,
    )


def _audit(
    session: Session,
    task: ReviewTaskRow,
    actor: Actor,
    now: datetime,
    action: str,
    new_value: dict[str, Any],
    old_value: Any = None,
) -> None:
    append_audit(
        session,
        task.tenant_id,
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=ENTITY,
            entity_id=str(task.id),
            case_version=task.case_version,
            old_value=old_value,
            new_value=new_value,
        ),
        now=now,
    )


def open_tasks_of(session: Session, case_id: UUID) -> Sequence[ReviewTaskRow]:
    return session.scalars(
        select(ReviewTaskRow)
        .where(ReviewTaskRow.case_id == case_id, ReviewTaskRow.status == Status.OPEN.value)
        .order_by(ReviewTaskRow.created_at)
    ).all()
