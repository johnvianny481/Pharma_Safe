"""Database access with tenant isolation enforced by PostgreSQL row-level security.

Every tenant table has an RLS policy comparing ``tenant_id`` to ``app_current_tenant()``,
which reads the transaction-local setting ``app.tenant_id``. :class:`Database` sets that
value at the start of every transaction from the session's tenant, so a query can only
ever see or write its own tenant's rows. A session opened without a tenant sees nothing
(the policy fails closed), it does not see everything.

The application connects as a non-owner role without BYPASSRLS. The schema owner is
used only by migrations and operator tooling.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import Connection, Engine, create_engine, event, text
from sqlalchemy.orm import Session, SessionTransaction, sessionmaker

TENANT_INFO_KEY = "tenant_id"


class Database:
    def __init__(self, url: str, **engine_kwargs: Any) -> None:
        self.engine: Engine = create_engine(url, pool_pre_ping=True, **engine_kwargs)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)
        event.listen(self._sessions, "after_begin", _apply_tenant)

    @contextmanager
    def tenant_session(self, tenant_id: UUID) -> Iterator[Session]:
        """A unit of work for one tenant: commits on success, rolls back on error."""
        with self._sessions(info={TENANT_INFO_KEY: str(tenant_id)}) as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    @contextmanager
    def system_session(self) -> Iterator[Session]:
        """A session with no tenant. RLS hides all tenant rows; only shared tables are visible."""
        with self._sessions() as session:
            try:
                yield session
                session.commit()
            except BaseException:
                session.rollback()
                raise

    def dispose(self) -> None:
        self.engine.dispose()


def _apply_tenant(session: Session, _tx: SessionTransaction, connection: Connection) -> None:
    tenant_id = session.info.get(TENANT_INFO_KEY)
    if tenant_id is not None:
        # is_local=true: the setting ends with the transaction, so pooled connections never
        # carry one tenant's context into another tenant's request.
        connection.execute(
            text("select set_config('app.tenant_id', :tenant_id, true)"), {"tenant_id": tenant_id}
        )
