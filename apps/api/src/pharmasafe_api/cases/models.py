"""ORM mappings for the cases module's tables (schema in migrations ``0002`` and ``0005``)."""

from datetime import date, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, Identity, Integer, LargeBinary, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import UserDefinedType

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
    merged_into: Mapped[UUID | None]
    merged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    merged_by: Mapped[str | None] = mapped_column(Text)
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


# ---------------------------------------------------------------- AI results (migration 0005)


class Vector(UserDefinedType[list[float]]):
    """pgvector's ``vector`` type, as a list of floats (no extra driver package needed)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "vector"

    def bind_processor(self, dialect: Any) -> Any:
        def process(value: list[float] | None) -> str | None:
            return None if value is None else "[" + ",".join(f"{v:.6f}" for v in value) + "]"

        return process

    def result_processor(self, dialect: Any, coltype: Any) -> Any:
        def process(value: str | None) -> list[float] | None:
            return None if value is None else [float(v) for v in value.strip("[]").split(",")]

        return process


class CaseAiRunRow(Base):
    __tablename__ = "case_ai_runs"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    bundle_version: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    automation_level: Mapped[str] = mapped_column(Text)
    flags: Mapped[list[str]] = mapped_column(ARRAY(Text))
    result: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CaseRedactionRow(Base):
    __tablename__ = "case_redactions"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    document_id: Mapped[str] = mapped_column(Text)
    key_id: Mapped[str] = mapped_column(Text)
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CaseVectorRow(Base):
    __tablename__ = "case_vectors"

    case_id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    drugs: Mapped[list[str]] = mapped_column(ARRAY(Text))
    reactions: Mapped[list[str]] = mapped_column(ARRAY(Text))
    keys: Mapped[dict[str, Any]] = mapped_column(JSONB)
    embedding: Mapped[list[float]] = mapped_column(Vector())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
