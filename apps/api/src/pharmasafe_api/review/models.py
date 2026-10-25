"""ORM mappings for the review module's tables (schema in migration ``0004``)."""

from datetime import date, datetime
from uuid import UUID

from sqlalchemy import BigInteger, Date, DateTime, Identity, Integer, Text
from sqlalchemy.orm import Mapped, mapped_column

from pharmasafe_platform.models import Base


class ReviewTaskRow(Base):
    __tablename__ = "review_tasks"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="open")
    assignee: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_by: Mapped[str | None] = mapped_column(Text)
    close_reason: Mapped[str | None] = mapped_column(Text)
    signature_id: Mapped[UUID | None]


class DeadlineEscalationRow(Base):
    __tablename__ = "deadline_escalations"

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    tenant_id: Mapped[UUID]
    case_id: Mapped[UUID]
    case_version: Mapped[int] = mapped_column(Integer)
    regulator: Mapped[str] = mapped_column(Text)
    rule_id: Mapped[str] = mapped_column(Text)
    due_date: Mapped[date] = mapped_column(Date)
    level: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
