"""Tenant admin API: ``/admin/tenant`` settings, for the tenant admin role."""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from pharmasafe_api.admin import service
from pharmasafe_api.deps import TenantSession, require_roles
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.auth import Principal, Role

router = APIRouter(prefix="/admin", tags=["admin"])

TenantAdmin = Annotated[Principal, Depends(require_roles(Role.TENANT_ADMIN))]


class TenantSettingsOut(BaseModel):
    slug: str
    name: str
    region: str
    regulators: list[str]
    available_regulators: list[str]
    ai_automation_level: str
    qc_sample_rate: Decimal
    intake_mailbox: str | None
    ai_monthly_budget_usd: Decimal


class TenantSettingsIn(BaseModel):
    """Only the fields present are changed."""

    ai_automation_level: str | None = None
    qc_sample_rate: Decimal | None = None
    regulators: list[str] | None = Field(default=None, max_length=20)
    intake_mailbox: str | None = None
    ai_monthly_budget_usd: Decimal | None = None
    reason: str | None = Field(default=None, max_length=2000)


def _out(settings: service.TenantSettings, request: Request) -> TenantSettingsOut:
    return TenantSettingsOut(
        **{**settings.__dict__, "regulators": list(settings.regulators)},
        available_regulators=sorted(request.app.state.clock_rules.regulators),
    )


@router.get("/tenant", response_model=TenantSettingsOut)
def get_tenant(principal: TenantAdmin, session: TenantSession, request: Request) -> Any:
    return _out(service.settings_of(session, principal.require_tenant()), request)


@router.patch("/tenant", response_model=TenantSettingsOut)
def update_tenant(
    body: TenantSettingsIn, principal: TenantAdmin, session: TenantSession, request: Request
) -> Any:
    changes = body.model_dump(exclude_unset=True, exclude={"reason"})
    try:
        updated = service.update_settings(
            session,
            principal.require_tenant(),
            changes,
            known_regulators=request.app.state.clock_rules.regulators,
            actor=Actor.from_principal(principal),
            now=datetime.now(UTC),
            reason=body.reason,
        )
    except service.InvalidSettings as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    return _out(updated, request)
