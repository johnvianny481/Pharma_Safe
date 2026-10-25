"""Review task APIs: the work queue, claiming, assignment, completion and return."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.router import CASE_ROLES
from pharmasafe_api.deps import TenantSession, require_roles
from pharmasafe_api.review import service
from pharmasafe_api.review.models import ReviewTaskRow
from pharmasafe_platform.auth import AuthenticationError, AuthorizationError, Principal
from pharmasafe_platform.signatures import ReauthenticationRequired, ReauthenticationReused

router = APIRouter(prefix="/tasks", tags=["review"])

Worker = Annotated[Principal, Depends(require_roles(*CASE_ROLES))]


class TaskOut(BaseModel):
    id: UUID
    case_id: UUID
    case_number: str | None = None
    case_version: int
    kind: str
    status: str
    assignee: str | None
    created_at: datetime
    closed_at: datetime | None
    closed_by: str | None
    close_reason: str | None
    signature_id: UUID | None
    next_due: date | None = None
    case_stage: str | None = None
    flags: list[str] = []


class Assign(BaseModel):
    assignee: str = Field(min_length=1, max_length=200, description="The user's subject id")


class Complete(BaseModel):
    reauth_token: str = Field(description="Access token from a fresh login (prompt=login)")
    reason: str | None = Field(default=None, max_length=2000)


class Return(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)


@router.get("", response_model=list[TaskOut])
def work_queue(
    principal: Worker,
    session: TenantSession,
    mine: bool = False,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[TaskOut]:
    """Open tasks for the caller's roles and scope, by deadline risk (earliest due first)."""
    return [
        _out(item.task, item.case_number, item.next_due).model_copy(
            update={"case_stage": item.stage, "flags": item.flags}
        )
        for item in service.queue(session, principal, mine_only=mine, limit=limit)
    ]


@router.post("/{task_id}/claim", response_model=TaskOut)
def claim(task_id: UUID, principal: Worker, session: TenantSession) -> TaskOut:
    with _errors():
        return _out(service.claim(session, task_id, principal, datetime.now(UTC)))


@router.post("/{task_id}/assign", response_model=TaskOut)
def assign(task_id: UUID, body: Assign, principal: Worker, session: TenantSession) -> TaskOut:
    with _errors():
        return _out(service.assign(session, task_id, body.assignee, principal, datetime.now(UTC)))


@router.post("/{task_id}/complete", response_model=TaskOut)
def complete(
    task_id: UUID, body: Complete, principal: Worker, session: TenantSession, request: Request
) -> TaskOut:
    """Sign the task's step (e-signature with fresh re-authentication) and close it."""
    try:
        reauth = request.app.state.verifier.verify(body.reauth_token)
    except AuthenticationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=f"re-authentication failed: {exc}"
        ) from exc
    ctx = cases.CaseContext(registry=request.app.state.clock_rules, now=datetime.now(UTC))
    with _errors():
        task = service.complete(
            session, task_id, principal, reauth, request.app.state.signatures, ctx, body.reason
        )
    return _out(task)


@router.post("/{task_id}/return", response_model=TaskOut)
def return_task(task_id: UUID, body: Return, principal: Worker, session: TenantSession) -> TaskOut:
    """Send the case back to data entry with a reason (review and approval tasks only)."""
    with _errors():
        return _out(
            service.return_task(session, task_id, principal, body.reason, datetime.now(UTC))
        )


@contextmanager
def _errors() -> Iterator[None]:
    """Map review and case-store errors to HTTP responses."""
    try:
        yield
    except service.TaskNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="task not found") from exc
    except (service.TaskConflict, cases.CaseConflict, ReauthenticationReused) as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    except cases.ApprovalBlocked as exc:
        issues = [{"code": i.code, "path": i.path, "message": i.message} for i in exc.issues]
        detail = {"message": str(exc), "issues": issues}
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail) from exc
    except ReauthenticationRequired as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from exc
    except AuthorizationError as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc


def _out(
    task: ReviewTaskRow, case_number: str | None = None, next_due: date | None = None
) -> TaskOut:
    out = TaskOut.model_validate(task, from_attributes=True)
    return out.model_copy(update={"case_number": case_number, "next_due": next_due})
