"""Audit ledger against real PostgreSQL: chaining, verification, immutability."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.audit import (
    GENESIS_HASH,
    Actor,
    AuditEvent,
    append_audit,
    entry_hash,
    list_entries,
    verify_chain,
)
from pharmasafe_platform.db import Database

ACTOR = Actor(id="u1", name="User One")


def _event(n: int, **kw: object) -> AuditEvent:
    fields: dict[str, object] = {
        "actor": ACTOR,
        "action": "case.field_changed",
        "entity_type": "case",
        "entity_id": f"case-{n}",
        "case_version": 1,
        "field": "patient.age",
        "old_value": n,
        "new_value": {"value": n + 1, "unit": "years"},
        "reason": "corrected",
    }
    fields.update(kw)
    return AuditEvent(**fields)  # type: ignore[arg-type]


def _append(db: Database, tenant: UUID, count: int) -> None:
    with db.tenant_session(tenant) as session:
        for n in range(count):
            append_audit(session, tenant, _event(n))


def _tamper(owner_engine: Engine, sql: str, **params: object) -> None:
    """Simulate a privileged attacker: bypass the append-only trigger and edit rows."""
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_append_only"))
        conn.execute(text(sql), params)
        conn.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_append_only"))


def test_entries_form_a_chain(db: Database, tenant_a: UUID) -> None:
    _append(db, tenant_a, 3)
    with db.tenant_session(tenant_a) as session:
        rows = sorted(list_entries(session), key=lambda r: r.seq)
        result = verify_chain(session, tenant_a)
    assert [r.seq for r in rows] == [1, 2, 3]
    assert rows[0].prev_hash == GENESIS_HASH
    assert rows[1].prev_hash == rows[0].hash
    assert rows[2].prev_hash == rows[1].hash
    assert result.ok
    assert result.entries_checked == 3


def test_empty_chain_verifies(db: Database, tenant_a: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert result.ok
    assert result.entries_checked == 0


def test_chains_are_independent_per_tenant(db: Database, tenant_a: UUID, tenant_b: UUID) -> None:
    _append(db, tenant_a, 2)
    _append(db, tenant_b, 1)
    with db.tenant_session(tenant_b) as session:
        (row,) = list_entries(session)
    assert row.seq == 1
    assert row.prev_hash == GENESIS_HASH


def test_detects_edited_entry(db: Database, owner_engine: Engine, tenant_a: UUID) -> None:
    _append(db, tenant_a, 3)
    _tamper(
        owner_engine,
        "UPDATE audit_ledger SET new_value = '{\"value\": 99}' WHERE tenant_id = :t AND seq = 2",
        t=tenant_a,
    )
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert not result.ok
    assert result.first_invalid_seq == 2
    assert result.problem == "entry content does not match its hash"


def test_detects_edit_with_recomputed_hash(
    db: Database, owner_engine: Engine, tenant_a: UUID
) -> None:
    """Rewriting an entry *and* its hash still breaks the link to the next entry."""
    _append(db, tenant_a, 3)
    with db.tenant_session(tenant_a) as session:
        row = next(r for r in list_entries(session) if r.seq == 2)
        forged = entry_hash(
            tenant_id=row.tenant_id, seq=row.seq, occurred_at=row.occurred_at,
            actor_id="someone-else", actor_name=row.actor_name, action=row.action,
            entity_type=row.entity_type, entity_id=row.entity_id, case_version=row.case_version,
            field=row.field, old_value=row.old_value, new_value=row.new_value,
            reason=row.reason, prev_hash=row.prev_hash,
        )  # fmt: skip
    _tamper(
        owner_engine,
        "UPDATE audit_ledger SET actor_id = 'someone-else', hash = :h WHERE tenant_id = :t AND seq = 2",
        h=forged,
        t=tenant_a,
    )
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert not result.ok
    assert result.first_invalid_seq == 3
    assert result.problem == "prev_hash does not match the previous entry's hash"


def test_detects_deleted_entry(db: Database, owner_engine: Engine, tenant_a: UUID) -> None:
    _append(db, tenant_a, 3)
    _tamper(owner_engine, "DELETE FROM audit_ledger WHERE tenant_id = :t AND seq = 2", t=tenant_a)
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert not result.ok
    assert result.first_invalid_seq == 2
    assert "missing" in (result.problem or "")


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_ledger SET reason = 'x'",
        "DELETE FROM audit_ledger",
        "TRUNCATE audit_ledger",
    ],
)
def test_app_role_cannot_modify_ledger(db: Database, tenant_a: UUID, statement: str) -> None:
    _append(db, tenant_a, 1)
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        db.tenant_session(tenant_a) as session,
    ):
        session.execute(text(statement))


@pytest.mark.parametrize(
    "statement",
    [
        "UPDATE audit_ledger SET reason = 'x' WHERE tenant_id = :t",
        "DELETE FROM audit_ledger WHERE tenant_id = :t",
        "TRUNCATE audit_ledger",
    ],
)
def test_even_owner_cannot_modify_ledger(
    db: Database, owner_engine: Engine, tenant_a: UUID, statement: str
) -> None:
    _append(db, tenant_a, 1)
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
        conn.execute(text(statement), {"t": tenant_a})


def test_concurrent_appends_keep_the_chain_intact(db: Database, tenant_a: UUID) -> None:
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: _append(db, tenant_a, 5), range(6)))
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert result.ok
    assert result.entries_checked == 30


def test_json_values_survive_jsonb_normalisation(db: Database, tenant_a: UUID) -> None:
    odd_values: list[object] = [1e20, 1.0, -0.0, 2.5, "ünïcødé", [1, "two", None], {"b": 1, "a": 2}]
    with db.tenant_session(tenant_a) as session:
        for value in odd_values:
            append_audit(session, tenant_a, _event(0, new_value=value))
    with db.tenant_session(tenant_a) as session:
        result = verify_chain(session, tenant_a)
    assert result.ok, result.problem


def test_rolled_back_change_leaves_no_audit_entry(db: Database, tenant_a: UUID) -> None:
    with pytest.raises(RuntimeError), db.tenant_session(tenant_a) as session:
        append_audit(session, tenant_a, _event(0))
        raise RuntimeError("the change failed")
    with db.tenant_session(tenant_a) as session:
        assert list_entries(session) == []
