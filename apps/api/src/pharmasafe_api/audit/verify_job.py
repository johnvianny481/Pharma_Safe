"""Verify every tenant's audit chain: ``python -m pharmasafe_api.audit.verify_job``.

Runs as the application role, one tenant session at a time, so RLS still applies. Exits
non-zero if any chain is broken, which the scheduler (a Temporal schedule from Phase 4)
turns into an alert. Archiving verified chain heads to WORM storage arrives in Phase 10.
"""

import logging
import sys

from sqlalchemy import select

from pharmasafe_platform.audit import ChainVerification, verify_chain
from pharmasafe_platform.db import Database
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.models import TenantRow
from pharmasafe_platform.settings import get_settings

log = logging.getLogger("audit.verify_job")


def verify_all(database: Database) -> list[ChainVerification]:
    with database.system_session() as session:
        tenant_ids = list(session.scalars(select(TenantRow.id).order_by(TenantRow.id)))
    results = []
    for tenant_id in tenant_ids:
        with database.tenant_session(tenant_id) as session:
            result = verify_chain(session, tenant_id)
        level = logging.INFO if result.ok else logging.ERROR
        log.log(
            level,
            "audit chain verified" if result.ok else f"audit chain broken: {result.problem}",
            extra={"tenant_id": str(tenant_id)},
        )
        results.append(result)
    return results


def main() -> int:
    settings = get_settings()
    configure_logging("audit-verify-job", settings.log_level)
    database = Database(settings.database_url)
    try:
        results = verify_all(database)
    finally:
        database.dispose()
    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
