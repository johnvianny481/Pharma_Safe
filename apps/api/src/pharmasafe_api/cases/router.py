"""Case APIs.

Clients never send provenance. A value set through the API is recorded as entered by the
caller (source ``human``, confirmed by them, now). ``confirm`` keeps a proposed value and
its evidence and records the caller as confirming it. AI and import sources only come
from internal pipelines (Phases 5 and 7), never from this API.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy.orm import Session

from pharmasafe_api.cases import service
from pharmasafe_api.cases.models import CaseClockRow, CaseRow, CaseVersionRow
from pharmasafe_api.deps import TenantSession, require_roles
from pharmasafe_domain.case import ItemId, SafetyCase
from pharmasafe_domain.changes import (
    AddItem,
    ChangeError,
    RemoveItem,
    SetField,
    apply_change,
    tracked_value_at,
)
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.checks import Issue, approval_blockers, check_case
from pharmasafe_domain.rules.seriousness import case_is_serious
from pharmasafe_domain.rules.validity import assess_validity
from pharmasafe_platform.auth import AuthenticationError, AuthorizationError, Principal, Role
from pharmasafe_platform.context import current_case_id
from pharmasafe_platform.signatures import ReauthenticationRequired, ReauthenticationReused
from pharmasafe_platform.telemetry import tag_current_span

router = APIRouter(prefix="/cases", tags=["cases"])

CASE_ROLES = (
    Role.INTAKE_PROCESSOR,
    Role.CASE_PROCESSOR,
    Role.QUALITY_REVIEWER,
    Role.MEDICAL_REVIEWER,
    Role.TENANT_ADMIN,
)
Reader = Annotated[Principal, Depends(require_roles(*CASE_ROLES))]
Creator = Annotated[Principal, Depends(require_roles(Role.INTAKE_PROCESSOR, Role.CASE_PROCESSOR))]
Editor = Annotated[
    Principal,
    Depends(require_roles(Role.CASE_PROCESSOR, Role.QUALITY_REVIEWER, Role.MEDICAL_REVIEWER)),
]
Approver = Annotated[
    Principal, Depends(require_roles(Role.QUALITY_REVIEWER, Role.MEDICAL_REVIEWER))
]
Auditor = Annotated[Principal, Depends(require_roles(Role.TENANT_ADMIN, Role.QUALITY_REVIEWER))]


# async so the context variable stays visible to the endpoint (see deps.get_principal).
async def _case_context(case_id: UUID) -> UUID:
    current_case_id.set(str(case_id))
    tag_current_span(case_id=str(case_id))
    return case_id


CaseId = Annotated[UUID, Depends(_case_context)]


# ---------------------------------------------------------------- request bodies


class SetOp(BaseModel):
    op: Literal["set"]
    path: str
    value: Any = Field(description="The new value; null clears the field.")


class ConfirmOp(BaseModel):
    op: Literal["confirm"]
    path: str


class AddOp(BaseModel):
    op: Literal["add"]
    path: str = Field(description="A repeating section, e.g. /reactions")
    id: ItemId
    drug_id: ItemId | None = Field(default=None, description="Causality items only")
    reaction_id: ItemId | None = Field(default=None, description="Causality items only")


class RemoveOp(BaseModel):
    op: Literal["remove"]
    path: str


EditOp = Annotated[SetOp | ConfirmOp | AddOp | RemoveOp, Field(discriminator="op")]
Reason = Annotated[str | None, Field(default=None, max_length=2000)]


class CreateCase(BaseModel):
    changes: list[EditOp] = Field(default_factory=list, max_length=500)
    reason: Reason = None


class EditVersion(BaseModel):
    expected_revision: int = Field(ge=0)
    changes: list[EditOp] = Field(min_length=1, max_length=500)
    reason: Reason = None


class Approve(BaseModel):
    reauth_token: str = Field(description="Access token from a fresh login (prompt=login)")
    reason: Reason = None


class StartFollowUp(BaseModel):
    receipt_date: date
    reason: Reason = None


# ---------------------------------------------------------------- responses


class CaseSummary(BaseModel):
    id: UUID
    case_number: str
    region: str
    product_codes: list[str]
    day_zero: date | None
    latest_version: int
    created_at: datetime


class VersionSummary(BaseModel):
    version: int
    status: str
    revision: int
    content_hash: str
    created_at: datetime
    created_by: str
    approved_at: datetime | None
    approved_by: str | None
    signature_id: UUID | None


class CaseDetail(CaseSummary):
    versions: list[VersionSummary]


class VersionDetail(VersionSummary):
    content: dict[str, Any]


class IssueOut(BaseModel):
    code: str
    path: str
    message: str
    severity: str


class ClockOut(BaseModel):
    regulator: str
    rule_id: str
    rule_set_version: str
    case_version: int
    clock_start: date
    due_date: date


class Assessment(BaseModel):
    valid: bool
    missing_criteria: list[str]
    serious: bool
    issues: list[IssueOut]
    approval_blockers: list[IssueOut]
    day_zero: date | None
    clocks: list[ClockOut]


class RebuiltVersion(BaseModel):
    version: int
    as_of: datetime | None
    changes_applied: int
    matches_stored: bool | None
    content: dict[str, Any]


# ---------------------------------------------------------------- endpoints


@router.post("", response_model=CaseDetail, status_code=status.HTTP_201_CREATED)
def create(
    body: CreateCase, principal: Creator, session: TenantSession, request: Request
) -> CaseDetail:
    ctx = _context(request)
    changes = _to_changes(SafetyCase(), body.changes, principal, ctx.now)
    with _errors():
        case, version = service.create_case(
            session, principal, SafetyCase(), ctx, reason=body.reason
        )
        if changes:
            service.apply_changes(
                session,
                case,
                1,
                expected_revision=version.revision,
                changes=changes,
                principal=principal,
                ctx=ctx,
                reason=body.reason,
            )
    return _detail(session, case)


@router.get("", response_model=list[CaseSummary])
def list_cases(
    principal: Reader, session: TenantSession, limit: Annotated[int, Query(ge=1, le=500)] = 100
) -> list[CaseSummary]:
    """Newest first, limited to the caller's tenant and data scope."""
    return [_summary(c) for c in service.list_cases(session, principal.scope, limit=limit)]


@router.get("/{case_id}", response_model=CaseDetail)
def get(case_id: CaseId, principal: Reader, session: TenantSession) -> CaseDetail:
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
    return _detail(session, case)


@router.get("/{case_id}/versions/{version}", response_model=VersionDetail)
def get_version(
    case_id: CaseId, version: int, principal: Reader, session: TenantSession
) -> VersionDetail:
    with _errors():
        service.get_case(session, case_id, principal.scope)
        row = service.get_version(session, case_id, version)
    return VersionDetail(**_version_summary(row).model_dump(), content=row.content)


@router.patch("/{case_id}/versions/{version}", response_model=VersionDetail)
def edit(
    case_id: CaseId,
    version: int,
    body: EditVersion,
    principal: Editor,
    session: TenantSession,
    request: Request,
) -> VersionDetail:
    ctx = _context(request)
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
        current = service.case_of(service.get_version(session, case_id, version))
        changes = _to_changes(current, body.changes, principal, ctx.now)
        row = service.apply_changes(
            session,
            case,
            version,
            expected_revision=body.expected_revision,
            changes=changes,
            principal=principal,
            ctx=ctx,
            reason=body.reason,
        )
    return VersionDetail(**_version_summary(row).model_dump(), content=row.content)


@router.get("/{case_id}/versions/{version}/assessment", response_model=Assessment)
def assessment(
    case_id: CaseId, version: int, principal: Reader, session: TenantSession, request: Request
) -> Assessment:
    """Deterministic rule results for one version: validity, seriousness, checks, clocks."""
    today = _context(request).today
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
        content = service.case_of(service.get_version(session, case_id, version))
    validity = assess_validity(content)
    return Assessment(
        valid=validity.valid,
        missing_criteria=sorted(validity.missing),
        serious=case_is_serious(content),
        issues=[_issue(i) for i in check_case(content, today=today)],
        approval_blockers=[_issue(i) for i in approval_blockers(content, today=today)],
        day_zero=case.day_zero,
        clocks=[_clock(c) for c in service.list_clocks(session, case_id)],
    )


@router.post("/{case_id}/versions/{version}/approve", response_model=VersionDetail)
def approve(
    case_id: CaseId,
    version: int,
    body: Approve,
    principal: Approver,
    session: TenantSession,
    request: Request,
) -> VersionDetail:
    """Approve and lock a version. Needs a fresh re-authentication (e-signature)."""
    try:
        reauth = request.app.state.verifier.verify(body.reauth_token)
    except AuthenticationError as exc:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail=f"re-authentication failed: {exc}"
        ) from exc
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
        row = service.approve_version(
            session,
            case,
            version,
            signer=principal,
            reauth=reauth,
            signatures=request.app.state.signatures,
            ctx=_context(request),
            reason=body.reason,
        )
    return VersionDetail(**_version_summary(row).model_dump(), content=row.content)


@router.post(
    "/{case_id}/follow-ups", response_model=VersionDetail, status_code=status.HTTP_201_CREATED
)
def follow_up(
    case_id: CaseId,
    body: StartFollowUp,
    principal: Creator,
    session: TenantSession,
    request: Request,
) -> VersionDetail:
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
        row = service.start_follow_up(
            session,
            case,
            receipt_date=body.receipt_date,
            principal=principal,
            ctx=_context(request),
            reason=body.reason,
        )
    return VersionDetail(**_version_summary(row).model_dump(), content=row.content)


@router.get("/{case_id}/versions/{version}/rebuild", response_model=RebuiltVersion)
def rebuild(
    case_id: CaseId,
    version: int,
    principal: Auditor,
    session: TenantSession,
    as_of: AwareDatetime | None = None,
) -> RebuiltVersion:
    """Rebuild a version from the audit trail, now or as it was at ``as_of``."""
    with _errors():
        case = service.get_case(session, case_id, principal.scope)
        result = service.rebuild_version(session, case, version, as_of=as_of)
    return RebuiltVersion(
        version=version,
        as_of=as_of,
        changes_applied=result.changes_applied,
        matches_stored=result.matches_stored,
        content=result.case.to_json(),
    )


# ---------------------------------------------------------------- helpers


def _context(request: Request) -> service.CaseContext:
    return service.CaseContext(registry=request.app.state.clock_rules, now=datetime.now(UTC))


def _to_changes(
    base: SafetyCase,
    ops: list[SetOp | ConfirmOp | AddOp | RemoveOp],
    principal: Principal,
    now: datetime,
) -> list[service.Change]:
    """Turn API operations into domain changes with server-side provenance."""
    changes: list[service.Change] = []
    preview = base
    for op in ops:
        change: service.Change
        try:
            if isinstance(op, SetOp):
                value = None
                if op.value is not None:
                    entered = Tracked[Any].human(op.value, by=principal.subject, at=now)
                    value = entered.model_dump(mode="json")
                change = SetField(path=op.path, value=value)
            elif isinstance(op, ConfirmOp):
                existing = tracked_value_at(preview, op.path)
                if existing is None:
                    raise ChangeError(f"{op.path} has no value to confirm")
                confirmed = Tracked[Any].model_validate(existing).confirm(principal.subject, now)
                change = SetField(path=op.path, value=confirmed.model_dump(mode="json"))
            elif isinstance(op, AddOp):
                links = {"drug_id": op.drug_id, "reaction_id": op.reaction_id}
                item = {"id": op.id} | {k: v for k, v in links.items() if v is not None}
                change = AddItem(path=op.path, item=item)
            else:
                change = RemoveItem(path=op.path)
            preview, _ = apply_change(preview, change)
        except (ChangeError, ValueError) as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
        changes.append(change)
    return changes


@contextmanager
def _errors() -> Iterator[None]:
    """Map case-store errors to HTTP responses."""
    try:
        yield
    except Exception as exc:
        mapped = _http_error(exc)
        if mapped is None:
            raise
        raise mapped from exc


def _http_error(exc: BaseException) -> HTTPException | None:
    if isinstance(exc, service.CaseNotFound):
        return HTTPException(status.HTTP_404_NOT_FOUND, detail=str(exc))
    if isinstance(exc, service.CaseConflict | ReauthenticationReused):
        return HTTPException(status.HTTP_409_CONFLICT, detail=str(exc))
    if isinstance(exc, service.ApprovalBlocked):
        detail = {"message": str(exc), "issues": [_issue(i).model_dump() for i in exc.issues]}
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=detail)
    if isinstance(exc, service.InvalidChange):
        return HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc))
    if isinstance(exc, ReauthenticationRequired):
        return HTTPException(status.HTTP_401_UNAUTHORIZED, detail=str(exc))
    if isinstance(exc, AuthorizationError):
        return HTTPException(status.HTTP_403_FORBIDDEN, detail=str(exc))
    return None


def _summary(case: CaseRow) -> CaseSummary:
    return CaseSummary.model_validate(case, from_attributes=True)


def _version_summary(row: CaseVersionRow) -> VersionSummary:
    return VersionSummary.model_validate(row, from_attributes=True)


def _detail(session: Session, case: CaseRow) -> CaseDetail:
    versions = [_version_summary(v) for v in service.list_versions(session, case.id)]
    return CaseDetail(**_summary(case).model_dump(), versions=versions)


def _issue(issue: Issue) -> IssueOut:
    return IssueOut(
        code=issue.code, path=issue.path, message=issue.message, severity=issue.severity.value
    )


def _clock(row: CaseClockRow) -> ClockOut:
    return ClockOut.model_validate(row, from_attributes=True)
