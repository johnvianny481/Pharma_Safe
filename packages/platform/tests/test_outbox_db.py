"""Transactional outbox: events commit with the change, are relayed in order, and retried."""

from uuid import UUID

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.db import Database
from pharmasafe_platform.models import OutboxRow
from pharmasafe_platform.outbox import (
    InMemoryPublisher,
    OutboxEvent,
    OutboxMessage,
    OutboxRelay,
    enqueue,
)


def _event(n: int) -> OutboxEvent:
    return OutboxEvent(
        topic="cases", event_type="test.event", aggregate_id=f"c{n}", payload={"n": n}
    )


def _pending(db: Database, tenant: UUID) -> list[OutboxRow]:
    with db.tenant_session(tenant) as session:
        return list(
            session.scalars(
                select(OutboxRow).where(OutboxRow.published_at.is_(None)).order_by(OutboxRow.seq)
            )
        )


def test_event_commits_or_rolls_back_with_the_change(db: Database, tenant_a: UUID) -> None:
    with pytest.raises(RuntimeError), db.tenant_session(tenant_a) as session:
        append_audit(session, tenant_a, AuditEvent(Actor("u", "U"), "x", "case", "c1"))
        enqueue(session, tenant_a, _event(1))
        raise RuntimeError("the change fails after the event was written")
    assert _pending(db, tenant_a) == []

    with db.tenant_session(tenant_a) as session:
        enqueue(session, tenant_a, _event(2))
    assert [r.payload for r in _pending(db, tenant_a)] == [{"n": 2}]


def test_relay_publishes_each_tenants_events_in_order_once(
    db: Database, tenant_a: UUID, tenant_b: UUID
) -> None:
    with db.tenant_session(tenant_a) as session:
        for n in range(3):
            enqueue(session, tenant_a, _event(n))
    with db.tenant_session(tenant_b) as session:
        enqueue(session, tenant_b, _event(9))

    publisher = InMemoryPublisher()
    relay = OutboxRelay(db, publisher)
    relay.run_once()
    mine = [m for m in publisher.messages if m.tenant_id in (tenant_a, tenant_b)]
    assert [(m.tenant_id, m.payload["n"]) for m in mine if m.tenant_id == tenant_a] == [
        (tenant_a, 0), (tenant_a, 1), (tenant_a, 2)
    ]  # fmt: skip
    assert [m.payload["n"] for m in mine if m.tenant_id == tenant_b] == [9]
    assert _pending(db, tenant_a) == []
    assert _pending(db, tenant_b) == []

    before = len(publisher.messages)
    relay.run_once()
    assert len(publisher.messages) == before  # nothing is published twice


class _FlakyPublisher:
    def __init__(self, fail_on: int) -> None:
        self.fail_on = fail_on
        self.seen: list[OutboxMessage] = []

    def publish(self, message: OutboxMessage) -> None:
        if message.payload.get("n") == self.fail_on:
            raise ConnectionError("broker unavailable")
        self.seen.append(message)


def test_failed_publish_is_recorded_and_later_events_wait(db: Database, tenant_a: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        for n in range(3):
            enqueue(session, tenant_a, _event(n))

    flaky = _FlakyPublisher(fail_on=1)
    OutboxRelay(db, flaky).run_once()
    assert [m.payload["n"] for m in flaky.seen if m.tenant_id == tenant_a] == [0]
    pending = _pending(db, tenant_a)
    assert [r.payload["n"] for r in pending] == [1, 2]  # 2 did not overtake 1
    assert pending[0].attempts == 1
    assert pending[0].last_error == "ConnectionError: broker unavailable"

    flaky.fail_on = -1
    OutboxRelay(db, flaky).run_once()
    assert [m.payload["n"] for m in flaky.seen if m.tenant_id == tenant_a] == [0, 1, 2]


def test_app_role_can_only_mark_events_published(db: Database, tenant_a: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        enqueue(session, tenant_a, _event(1))
    with pytest.raises(DBAPIError, match="permission denied"), db.tenant_session(tenant_a) as s:
        s.execute(text("UPDATE outbox SET payload = '{}'"))
    with pytest.raises(DBAPIError, match="permission denied"), db.tenant_session(tenant_a) as s:
        s.execute(text("DELETE FROM outbox"))
