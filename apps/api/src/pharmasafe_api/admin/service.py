"""Tenant self-administration: the settings a tenant admin may change, audited.

Settings: AI automation level, QC sampling rate for non-serious cases, the regulators
whose clock rules apply, the intake mailbox, and the monthly AI budget. Row-level
security (migration ``0006``) lets the app update only the caller's own tenant row,
and only these columns. Users, roles and data scopes stay in the identity provider.

Every change is one audit entry per field, with the old and the new value.
"""

import re
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, column, select, table
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pharmasafe_domain.ai.automation import Level
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.models import TenantRow
from pharmasafe_platform.settings import get_settings

_MAILBOX = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
# The gateway's budget table (it owns the ORM model; the API only reads and sets limits).
_BUDGETS = table("ai_tenant_budgets", column("tenant_id"), column("monthly_limit_usd", Numeric))


class InvalidSettings(ValueError):
    pass


@dataclass(frozen=True)
class TenantSettings:
    slug: str
    name: str
    region: str
    regulators: tuple[str, ...]
    ai_automation_level: str
    qc_sample_rate: Decimal
    intake_mailbox: str | None
    ai_monthly_budget_usd: Decimal


def _budget(session: Session, tenant_id: UUID) -> Decimal:
    limit = session.scalar(
        select(_BUDGETS.c.monthly_limit_usd).where(_BUDGETS.c.tenant_id == tenant_id)
    )
    return Decimal(limit) if limit is not None else get_settings().ai_default_monthly_budget_usd


def settings_of(session: Session, tenant_id: UUID) -> TenantSettings:
    tenant = session.get_one(TenantRow, tenant_id)
    return TenantSettings(
        slug=tenant.slug,
        name=tenant.name,
        region=tenant.region,
        regulators=tuple(tenant.regulators),
        ai_automation_level=tenant.ai_automation_level,
        qc_sample_rate=tenant.qc_sample_rate,
        intake_mailbox=tenant.intake_mailbox,
        ai_monthly_budget_usd=_budget(session, tenant_id),
    )


def update_settings(
    session: Session,
    tenant_id: UUID,
    changes: dict[str, Any],
    *,
    known_regulators: frozenset[str],
    actor: Actor,
    now: datetime,
    reason: str | None,
) -> TenantSettings:
    """Apply the given changes (only fields present are changed), validated and audited."""
    current = settings_of(session, tenant_id)
    wanted = replace(current, **_validated(changes, known_regulators))
    tenant = session.get_one(TenantRow, tenant_id)
    old, new = asdict(current), asdict(wanted)
    for name in ("ai_automation_level", "qc_sample_rate", "intake_mailbox"):
        setattr(tenant, name, new[name])
    tenant.regulators = list(wanted.regulators)
    if wanted.ai_monthly_budget_usd != current.ai_monthly_budget_usd:
        statement = insert(_BUDGETS).values(
            tenant_id=tenant_id, monthly_limit_usd=wanted.ai_monthly_budget_usd
        )
        session.execute(
            statement.on_conflict_do_update(
                index_elements=["tenant_id"],
                set_={"monthly_limit_usd": wanted.ai_monthly_budget_usd},
            )
        )
    session.flush()
    for name in changes:
        if old[name] != new[name]:
            append_audit(
                session,
                tenant_id,
                AuditEvent(
                    actor=actor,
                    action="tenant.settings_changed",
                    entity_type="tenant",
                    entity_id=str(tenant_id),
                    field=name,
                    old_value=_jsonable(old[name]),
                    new_value=_jsonable(new[name]),
                    reason=reason,
                ),
                now=now,
            )
    return wanted


def _validated(changes: dict[str, Any], known_regulators: frozenset[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, value in changes.items():
        if name == "ai_automation_level":
            if value not in {level.value for level in Level}:
                raise InvalidSettings("automation level must be L0, L1 or L2")
            out[name] = value
        elif name == "qc_sample_rate":
            rate = Decimal(str(value))
            if not Decimal(0) <= rate <= Decimal(1):
                raise InvalidSettings("the QC sampling rate must be between 0 and 1")
            out[name] = rate.quantize(Decimal("0.001"))
        elif name == "regulators":
            regulators = tuple(dict.fromkeys(str(r).upper() for r in value))
            unknown = set(regulators) - known_regulators
            if unknown or not regulators:
                raise InvalidSettings(
                    f"regulators must be some of {', '.join(sorted(known_regulators))}"
                )
            out[name] = regulators
        elif name == "intake_mailbox":
            if value is not None and not _MAILBOX.match(str(value)):
                raise InvalidSettings("the intake mailbox must be an email address")
            out[name] = value
        elif name == "ai_monthly_budget_usd":
            budget = Decimal(str(value))
            if budget < 0:
                raise InvalidSettings("the AI budget cannot be negative")
            out[name] = budget.quantize(Decimal("0.01"))
        else:
            raise InvalidSettings(f"{name} cannot be changed here")
    return out


def _jsonable(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return list(value)
    return value
