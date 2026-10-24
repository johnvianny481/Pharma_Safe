"""Row-level security: one tenant can never read or write another tenant's rows."""

from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.audit import Actor, AuditEvent, append_audit, list_entries
from pharmasafe_platform.db import Database
from pharmasafe_platform.models import AuditLedgerRow

EVENT = AuditEvent(
    actor=Actor("u1", "User One"), action="test.event", entity_type="case", entity_id="c1"
)


def test_tenant_sees_only_its_own_rows(db: Database, tenant_a: UUID, tenant_b: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        append_audit(session, tenant_a, EVENT)
    with db.tenant_session(tenant_b) as session:
        append_audit(session, tenant_b, EVENT)
        append_audit(session, tenant_b, EVENT)

    with db.tenant_session(tenant_a) as session:
        rows = list_entries(session)
        # Even an explicit filter for the other tenant returns nothing.
        explicit = session.execute(
            text("SELECT count(*) FROM audit_ledger WHERE tenant_id = :b"), {"b": tenant_b}
        ).scalar_one()
    assert {r.tenant_id for r in rows} == {tenant_a}
    assert len(rows) == 1
    assert explicit == 0


def test_tenant_cannot_write_into_another_tenant(
    db: Database, tenant_a: UUID, tenant_b: UUID
) -> None:
    forged = AuditLedgerRow(
        tenant_id=tenant_b, seq=1, occurred_at=datetime.now(UTC), actor_id="u1", actor_name="x",
        action="forged", entity_type="case", entity_id="c1", prev_hash="0" * 64, hash="a" * 64,
    )  # fmt: skip
    with pytest.raises(DBAPIError, match="row-level security"), db.tenant_session(tenant_a) as s:
        s.add(forged)
        s.flush()


def test_session_without_tenant_sees_nothing(db: Database, tenant_a: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        append_audit(session, tenant_a, EVENT)
    with db.system_session() as session:
        count = session.execute(text("SELECT count(*) FROM audit_ledger")).scalar_one()
    assert count == 0


def test_tenant_context_does_not_leak_across_pooled_transactions(
    test_database: object, tenant_a: UUID
) -> None:
    single = Database(test_database.app_url, pool_size=1, max_overflow=0)  # type: ignore[attr-defined]
    try:
        with single.tenant_session(tenant_a) as session:
            append_audit(session, tenant_a, EVENT)
        with single.system_session() as session:
            setting = session.execute(text("SELECT app_current_tenant()")).scalar_one()
            count = session.execute(text("SELECT count(*) FROM audit_ledger")).scalar_one()
        assert setting is None
        assert count == 0
    finally:
        single.dispose()


def test_unknown_tenant_sees_nothing(db: Database, tenant_a: UUID) -> None:
    with db.tenant_session(tenant_a) as session:
        append_audit(session, tenant_a, EVENT)
    with db.tenant_session(uuid4()) as session:
        assert list_entries(session) == []


def test_every_tenant_table_has_forced_rls(owner_engine: Engine) -> None:
    """Guards future migrations: any table with tenant_id must be row-level secured."""
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity,
                       (SELECT count(*) FROM pg_policy p WHERE p.polrelid = c.oid) AS policies
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
                WHERE c.relkind = 'r'
                  AND EXISTS (SELECT 1 FROM pg_attribute a
                              WHERE a.attrelid = c.oid AND a.attname = 'tenant_id' AND NOT a.attisdropped)
            """)
        ).all()
    assert {r.relname for r in rows} >= {"audit_ledger", "e_signatures"}
    for row in rows:
        assert row.relrowsecurity, f"{row.relname} has RLS disabled"
        assert row.relforcerowsecurity, f"{row.relname} does not force RLS"
        assert row.policies >= 1, f"{row.relname} has no RLS policy"


def test_app_role_cannot_bypass_rls(db: Database) -> None:
    with db.system_session() as session:
        role = session.execute(
            text("SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        ).one()
        owned = session.execute(
            text(
                "SELECT count(*) FROM pg_tables WHERE schemaname = 'public' AND tableowner = current_user"
            )
        ).scalar_one()
    assert not role.rolsuper
    assert not role.rolbypassrls
    assert owned == 0


def test_app_role_cannot_create_tenants(db: Database) -> None:
    with pytest.raises(DBAPIError, match="permission denied"), db.system_session() as session:
        session.execute(
            text("INSERT INTO tenants (id, slug, name, region) VALUES (:id, 'x', 'x', 'eu')"),
            {"id": uuid4()},
        )
