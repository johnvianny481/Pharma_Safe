from uuid import UUID

from sqlalchemy import Engine, text

from pharmasafe_api.audit.verify_job import verify_all
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.db import Database


def _record(db: Database, tenant: UUID, count: int) -> None:
    with db.tenant_session(tenant) as session:
        for _ in range(count):
            append_audit(
                session, tenant, AuditEvent(Actor("u1", "User One"), "test.event", "case", "c1")
            )


def test_job_verifies_each_tenant_separately(
    db: Database, owner_engine: Engine, tenant_a: UUID, tenant_b: UUID
) -> None:
    _record(db, tenant_a, 2)
    _record(db, tenant_b, 2)
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE audit_ledger DISABLE TRIGGER audit_ledger_append_only"))
        conn.execute(
            text("UPDATE audit_ledger SET reason = 'tampered' WHERE tenant_id = :t AND seq = 1"),
            {"t": tenant_b},
        )
        conn.execute(text("ALTER TABLE audit_ledger ENABLE TRIGGER audit_ledger_append_only"))

    results = {r.tenant_id: r for r in verify_all(db)}
    assert results[tenant_a].ok
    assert results[tenant_a].entries_checked == 2
    assert not results[tenant_b].ok
    assert results[tenant_b].first_invalid_seq == 1
