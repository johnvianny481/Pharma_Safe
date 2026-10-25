"""ORM mappings for the cases module's tables (schema in migration ``0002``)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, Identity, Integer, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from pharmasafe_platform.models import Base


class CaseRow(Base):
    __tablename__ = "cases"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(BigInteger)
    case_number: Mapped[str] = mapped_column(Text)
    region: Mapped[str] = mapped_column(Text)
    product_codes: Mapped[list[str]] = mapped_column(ARRAY(Text))
    day_zero: Mapped[date | None] = mapped_column(Date)
    latest_version: Mapped[int] = mapped_column(Integer)
    workflow_stage: Mapped[str] = mapped_column(Text, default="received")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text)


class CaseVersionRow(Base):
    __tablename__ = "case_versions"

    case_id: Mapped[UUID] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[UUID]
    status: Mapped[str] = mapped_column(Text)
    schema_version: Mapped[int] = mapped_column(Integer)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB)
    content_hash: Mapped[str] = mapped_column(String(64))
    revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str] = mapped_column(Text)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_by: Mapped[str | None] = mapped_column(Text)
    signature_id: Mapped[UUID | None]


class CaseClockRow(Base):
    __tablename__ = "case_clocks"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    regulator: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    rule_set_version: Mapped[str] = mapped_column(Text)
    clock_start: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
