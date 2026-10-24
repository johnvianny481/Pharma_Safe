"""Tamper-evident audit ledger.

Each tenant has its own hash chain. Every entry stores the hash of the tenant's previous
entry (``prev_hash``) and a hash of its own content plus ``prev_hash``. Changing, removing
or reordering any entry breaks the chain from that point on, and :func:`verify_chain`
reports where.

Immutability is enforced in the database, not here: the application role has only SELECT
and INSERT on the table, and triggers reject UPDATE, DELETE and TRUNCATE for every role
including the owner (see migration ``0001``). Detecting removal of the newest entries
needs an external anchor of the chain head; the nightly WORM archive provides it (Phase 10).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, text
from sqlalchemy.orm import Session

from pharmasafe_platform.auth import Principal
from pharmasafe_platform.hashing import HASH_HEX_LENGTH, canonical_json, sha256_hex
from pharmasafe_platform.models import AuditLedgerRow

GENESIS_HASH = "0" * HASH_HEX_LENGTH


@dataclass(frozen=True)
class Actor:
    id: str
    name: str

    @classmethod
    def from_principal(cls, principal: Principal) -> "Actor":
        return cls(id=principal.subject, name=principal.display_name)


SYSTEM_ACTOR = Actor(id="system", name="PharmaSafe system")


@dataclass(frozen=True)
class AuditEvent:
    """What happened. ``reason`` is required by Part 11 for changes to regulated data."""

    actor: Actor
    action: str
    entity_type: str
    entity_id: str
    case_version: int | None = None
    field: str | None = None
    old_value: Any = None
    new_value: Any = None
    reason: str | None = None


def entry_hash(
    *,
    tenant_id: UUID,
    seq: int,
    occurred_at: datetime,
    actor_id: str,
    actor_name: str,
    action: str,
    entity_type: str,
    entity_id: str,
    case_version: int | None,
    field: str | None,
    old_value: Any,
    new_value: Any,
    reason: str | None,
    prev_hash: str,
) -> str:
    return sha256_hex(
        {
            "tenant_id": str(tenant_id),
            "seq": seq,
            "occurred_at": occurred_at.astimezone(UTC).isoformat(timespec="microseconds"),
            "actor_id": actor_id,
            "actor_name": actor_name,
            "action": action,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "case_version": case_version,
            "field": field,
            "old_value": old_value,
            "new_value": new_value,
            "reason": reason,
            "prev_hash": prev_hash,
        }
    )


def row_hash(row: AuditLedgerRow) -> str:
    return entry_hash(
        tenant_id=row.tenant_id,
        seq=row.seq,
        occurred_at=row.occurred_at,
        actor_id=row.actor_id,
        actor_name=row.actor_name,
        action=row.action,
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        case_version=row.case_version,
        field=row.field,
        old_value=row.old_value,
        new_value=row.new_value,
        reason=row.reason,
        prev_hash=row.prev_hash,
    )


def append_audit(
    session: Session, tenant_id: UUID, event: AuditEvent, *, now: datetime | None = None
) -> AuditLedgerRow:
    """Append one entry to the tenant's chain inside the caller's transaction.

    Appending in the same transaction as the change it records means the change and its
    audit entry commit or roll back together.
    """
    # Serialise appends per tenant so two transactions cannot both claim the same seq.
    session.execute(
        text("select pg_advisory_xact_lock(hashtextextended(:key, 0))"),
        {"key": f"audit:{tenant_id}"},
    )
    last = session.execute(
        select(AuditLedgerRow.seq, AuditLedgerRow.hash)
        .where(AuditLedgerRow.tenant_id == tenant_id)
        .order_by(AuditLedgerRow.seq.desc())
        .limit(1)
    ).first()
    seq = last.seq + 1 if last else 1
    prev_hash = last.hash if last else GENESIS_HASH
    occurred_at = (now or datetime.now(UTC)).astimezone(UTC)
    # Hash the values exactly as jsonb will return them (jsonb normalises some numbers),
    # so the verifier recomputes the same hash from what it reads back.
    old_value, new_value = _as_stored_json(session, event.old_value, event.new_value)

    row = AuditLedgerRow(
        tenant_id=tenant_id,
        seq=seq,
        occurred_at=occurred_at,
        actor_id=event.actor.id,
        actor_name=event.actor.name,
        action=event.action,
        entity_type=event.entity_type,
        entity_id=event.entity_id,
        case_version=event.case_version,
        field=event.field,
        old_value=old_value,
        new_value=new_value,
        reason=event.reason,
        prev_hash=prev_hash,
    )
    row.hash = row_hash(row)
    session.add(row)
    session.flush()
    return row


def _as_stored_json(session: Session, *values: Any) -> Sequence[Any]:
    params = {f"v{i}": None if v is None else canonical_json(v) for i, v in enumerate(values)}
    columns = ", ".join(f"cast(:v{i} as jsonb)" for i in range(len(values)))
    result = session.execute(text(f"select {columns}"), params).one()
    return tuple(result)


@dataclass(frozen=True)
class ChainVerification:
    tenant_id: UUID
    ok: bool
    entries_checked: int
    first_invalid_seq: int | None = None
    problem: str | None = None


def verify_chain(session: Session, tenant_id: UUID) -> ChainVerification:
    expected_seq = 1
    expected_prev = GENESIS_HASH
    rows = session.scalars(
        select(AuditLedgerRow)
        .where(AuditLedgerRow.tenant_id == tenant_id)
        .order_by(AuditLedgerRow.seq)
        .execution_options(yield_per=1000)
    )
    checked = 0
    for row in rows:
        problem = None
        if row.seq != expected_seq:
            problem = f"expected entry {expected_seq} but found {row.seq}: entries are missing"
        elif row.prev_hash != expected_prev:
            problem = "prev_hash does not match the previous entry's hash"
        elif row_hash(row) != row.hash:
            problem = "entry content does not match its hash"
        if problem:
            return ChainVerification(tenant_id, False, checked, expected_seq, problem)
        expected_prev = row.hash
        expected_seq += 1
        checked += 1
    return ChainVerification(tenant_id, True, checked)


def list_entries(
    session: Session,
    *,
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
) -> Sequence[AuditLedgerRow]:
    query = select(AuditLedgerRow).order_by(AuditLedgerRow.seq.desc()).limit(limit)
    if entity_type is not None:
        query = query.where(AuditLedgerRow.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(AuditLedgerRow.entity_id == entity_id)
    return session.scalars(query).all()
