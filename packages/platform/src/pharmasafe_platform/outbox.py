"""Transactional outbox.

A module that changes state and must tell other services about it writes the event to
the ``outbox`` table in the *same* transaction as the change (:func:`enqueue`). Either
both commit or neither does, so an event is never lost and never announces a change
that was rolled back.

:class:`OutboxRelay` later reads unpublished events and hands them to a
:class:`Publisher` (Azure Service Bus in production). Delivery is at least once: if the
relay crashes after publishing but before marking the event, it publishes it again.
Consumers must therefore deduplicate on the event ``id``. Events are published in
``seq`` order per tenant, and the relay stops at the first failure in a tenant's batch so
that later events do not overtake an earlier one.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from pharmasafe_platform.db import Database
from pharmasafe_platform.models import OutboxRow, TenantRow

MAX_ERROR_LENGTH = 1000


@dataclass(frozen=True)
class OutboxEvent:
    topic: str
    event_type: str
    aggregate_id: str
    # Identifiers and codes only, never personal data: events leave the database.
    payload: Mapping[str, Any]


@dataclass(frozen=True)
class OutboxMessage:
    id: UUID
    tenant_id: UUID
    topic: str
    event_type: str
    aggregate_id: str
    payload: Mapping[str, Any]
    created_at: datetime


def enqueue(
    session: Session, tenant_id: UUID, event: OutboxEvent, *, now: datetime | None = None
) -> UUID:
    """Record an event in the caller's transaction. Returns the event id."""
    row = OutboxRow(
        id=uuid4(),
        tenant_id=tenant_id,
        topic=event.topic,
        event_type=event.event_type,
        aggregate_id=event.aggregate_id,
        payload=dict(event.payload),
        created_at=(now or datetime.now(UTC)).astimezone(UTC),
        attempts=0,
    )
    session.add(row)
    session.flush()
    return row.id


class Publisher(Protocol):
    def publish(self, message: OutboxMessage) -> None:
        """Deliver one message or raise. Must be safe to call twice for the same id."""


class InMemoryPublisher:
    """For tests and local runs without a message broker."""

    def __init__(self) -> None:
        self.messages: list[OutboxMessage] = []

    def publish(self, message: OutboxMessage) -> None:
        self.messages.append(message)


class OutboxRelay:
    def __init__(self, database: Database, publisher: Publisher, *, batch_size: int = 100) -> None:
        self._database = database
        self._publisher = publisher
        self._batch_size = batch_size

    def run_once(self, tenant_ids: Iterable[UUID] | None = None) -> int:
        """Publish pending events (for every tenant, or only the given ones).

        Returns how many were published.
        """
        if tenant_ids is None:
            with self._database.system_session() as session:
                tenant_ids = session.scalars(select(TenantRow.id).order_by(TenantRow.id)).all()
        return sum(self._relay_tenant(tenant_id) for tenant_id in tenant_ids)

    def _relay_tenant(self, tenant_id: UUID) -> int:
        published = 0
        with self._database.tenant_session(tenant_id) as session:
            # SKIP LOCKED lets several relay replicas run without publishing the same row
            # at the same time.
            rows = session.scalars(
                select(OutboxRow)
                .where(OutboxRow.published_at.is_(None))
                .order_by(OutboxRow.seq)
                .limit(self._batch_size)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                try:
                    self._publisher.publish(_message(row))
                except Exception as exc:  # any broker error: record it and retry later
                    row.attempts += 1
                    row.last_error = f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH]
                    break
                row.published_at = datetime.now(UTC)
                published += 1
        return published


def _message(row: OutboxRow) -> OutboxMessage:
    return OutboxMessage(
        id=row.id,
        tenant_id=row.tenant_id,
        topic=row.topic,
        event_type=row.event_type,
        aggregate_id=row.aggregate_id,
        payload=row.payload,
        created_at=row.created_at,
    )
