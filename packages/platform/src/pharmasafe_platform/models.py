"""ORM mappings for platform-owned tables.

The schema itself (row-level security, grants, immutability triggers) is defined by the
Alembic migrations in ``apps/api/migrations``; these classes only map it.
"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import BigInteger, DateTime, Identity, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TenantRow(Base):
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    region: Mapped[str] = mapped_column(String(8))
    regulators: Mapped[list[str]] = mapped_column(ARRAY(Text))
    intake_mailbox: Mapped[str | None] = mapped_column(Text)
    qc_sample_rate: Mapped[Decimal] = mapped_column(Numeric(4, 3))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AuditLedgerRow(Base):
    __tablename__ = "audit_ledger"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    tenant_id: Mapped[UUID]
    seq: Mapped[int] = mapped_column(BigInteger)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    actor_id: Mapped[str] = mapped_column(Text)
    actor_name: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    case_version: Mapped[int | None] = mapped_column(Integer)
    field: Mapped[str | None] = mapped_column(Text)
    old_value: Mapped[Any] = mapped_column(JSONB)
    new_value: Mapped[Any] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    prev_hash: Mapped[str] = mapped_column(String(64))
    hash: Mapped[str] = mapped_column(String(64))


class ESignatureRow(Base):
    __tablename__ = "e_signatures"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[UUID]
    entity_type: Mapped[str] = mapped_column(Text)
    entity_id: Mapped[str] = mapped_column(Text)
    entity_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    meaning: Mapped[str] = mapped_column(Text)
    reason: Mapped[str | None] = mapped_column(Text)
    signer_id: Mapped[str] = mapped_column(Text)
    signer_name: Mapped[str] = mapped_column(Text)
    signed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    auth_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    reauth_token_id: Mapped[str] = mapped_column(Text)
    signature_hash: Mapped[str] = mapped_column(String(64))


class OutboxRow(Base):
    __tablename__ = "outbox"

    id: Mapped[UUID] = mapped_column(primary_key=True)
    seq: Mapped[int] = mapped_column(BigInteger, Identity(always=True), unique=True)
    tenant_id: Mapped[UUID]
    topic: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(Text)
    aggregate_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[Any] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
