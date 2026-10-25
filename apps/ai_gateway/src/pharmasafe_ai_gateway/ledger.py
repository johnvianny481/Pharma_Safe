"""Per-tenant budgets and the ledger of model calls (tables from migration ``0005``).

Every call the gateway makes, or refuses, is a row in ``ai_calls``: step, bundle and
prompt version, provider, model, tokens, cost, latency and outcome. It is the source for
cost per case and the per-tenant budget, and it is append-only.

**Budget.** Each tenant has a monthly limit in USD (``ai_tenant_budgets``, or the
default from settings). Before a call, the gateway refuses it if the month's spend plus
the call's worst-case cost (all of ``max_tokens`` used) would exceed the limit. Calls
running in parallel can overshoot by at most their own worst-case costs; the limit is a
cost control, not an accounting boundary.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID, uuid4

from sqlalchemy import DateTime, Integer, Numeric, Text, func, select
from sqlalchemy.orm import Mapped, mapped_column

from pharmasafe_platform.db import Database
from pharmasafe_platform.models import Base


class AiCallRow(Base):
    __tablename__ = "ai_calls"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[str] = mapped_column(Text)
    step: Mapped[str] = mapped_column(Text)
    bundle_version: Mapped[str] = mapped_column(Text)
    prompt_version: Mapped[str] = mapped_column(Text)
    provider: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer)
    fallback_used: Mapped[bool]
    outcome: Mapped[str] = mapped_column(Text)
    input_tokens: Mapped[int] = mapped_column(Integer)
    output_tokens: Mapped[int] = mapped_column(Integer)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AiTenantBudgetRow(Base):
    __tablename__ = "ai_tenant_budgets"

    tenant_id: Mapped[UUID] = mapped_column(primary_key=True)
    monthly_limit_usd: Mapped[Decimal] = mapped_column(Numeric(12, 2))


@dataclass(frozen=True)
class CallRecord:
    tenant_id: UUID
    case_id: str
    step: str
    bundle_version: str
    prompt_version: str
    provider: str | None
    model: str | None
    attempts: int
    fallback_used: bool
    outcome: str
    input_tokens: int
    output_tokens: int
    cost_usd: Decimal
    latency_ms: int


class Ledger:
    def __init__(self, database: Database, default_monthly_limit_usd: Decimal) -> None:
        self._database = database
        self._default = default_monthly_limit_usd

    def remaining(self, tenant_id: UUID, now: datetime) -> Decimal:
        month_start = now.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        with self._database.tenant_session(tenant_id) as session:
            limit = session.scalar(
                select(AiTenantBudgetRow.monthly_limit_usd).where(
                    AiTenantBudgetRow.tenant_id == tenant_id
                )
            )
            spent = session.scalar(
                select(func.coalesce(func.sum(AiCallRow.cost_usd), 0)).where(
                    AiCallRow.tenant_id == tenant_id, AiCallRow.created_at >= month_start
                )
            )
        return (limit if limit is not None else self._default) - Decimal(spent or 0)

    def record(self, call: CallRecord, now: datetime) -> UUID:
        call_id = uuid4()
        with self._database.tenant_session(call.tenant_id) as session:
            session.add(AiCallRow(id=call_id, created_at=now, **call.__dict__))
        return call_id
