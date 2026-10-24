from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from pharmasafe_api.deps import TenantSession, require_roles
from pharmasafe_platform.audit import list_entries, verify_chain
from pharmasafe_platform.auth import Principal, Role

router = APIRouter(prefix="/audit", tags=["audit"])

AuditReader = Annotated[Principal, Depends(require_roles(Role.TENANT_ADMIN, Role.QUALITY_REVIEWER))]
TenantAdmin = Annotated[Principal, Depends(require_roles(Role.TENANT_ADMIN))]


class AuditEntry(BaseModel):
    seq: int
    occurred_at: datetime
    actor_id: str
    actor_name: str
    action: str
    entity_type: str
    entity_id: str
    case_version: int | None
    field: str | None
    old_value: Any
    new_value: Any
    reason: str | None
    hash: str


class ChainStatus(BaseModel):
    ok: bool
    entries_checked: int
    first_invalid_seq: int | None
    problem: str | None


@router.get("/entries", response_model=list[AuditEntry])
def entries(
    principal: AuditReader,
    session: TenantSession,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
) -> list[AuditEntry]:
    """Newest first. Only the caller's tenant is ever visible (row-level security)."""
    rows = list_entries(session, entity_type=entity_type, entity_id=entity_id, limit=limit)
    return [AuditEntry.model_validate(row, from_attributes=True) for row in rows]


@router.post("/verify", response_model=ChainStatus)
def verify(principal: TenantAdmin, session: TenantSession) -> ChainStatus:
    result = verify_chain(session, principal.require_tenant())
    return ChainStatus(
        ok=result.ok,
        entries_checked=result.entries_checked,
        first_invalid_seq=result.first_invalid_seq,
        problem=result.problem,
    )
