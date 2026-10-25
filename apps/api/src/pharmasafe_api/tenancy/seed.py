"""Create the local demo tenants: ``python -m pharmasafe_api.tenancy.seed``.

Development only. The tenant ids match the ``tenant_id`` attributes of the demo users in
``deploy/keycloak/pharmasafe-realm.json``. Uses the owner connection because the app role
can only read the tenant directory.
"""

from uuid import UUID

from sqlalchemy import create_engine, text

from pharmasafe_platform.settings import Environment, get_settings

DEMO_TENANTS = (
    (
        UUID("11111111-1111-4111-8111-111111111111"),
        "acme-pharma",
        "Acme Pharma",
        "eu",
        "intake@acme.pharmasafe.local",
    ),
    (
        UUID("22222222-2222-4222-8222-222222222222"),
        "globex-bio",
        "Globex Bio",
        "us",
        "intake@globex.pharmasafe.local",
    ),
)


def main() -> None:
    settings = get_settings()
    if settings.environment not in (Environment.DEV, Environment.TEST):
        raise SystemExit("demo tenants are only for dev and test environments")
    engine = create_engine(settings.migration_database_url)
    with engine.begin() as conn:
        for tenant_id, slug, name, region, mailbox in DEMO_TENANTS:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, region, intake_mailbox)"
                    " VALUES (:id, :slug, :name, :region, :mailbox)"
                    " ON CONFLICT (id) DO UPDATE SET intake_mailbox = EXCLUDED.intake_mailbox"
                ),
                {"id": tenant_id, "slug": slug, "name": name, "region": region, "mailbox": mailbox},
            )
            print(f"tenant {slug} ({tenant_id}) ready")
    engine.dispose()


if __name__ == "__main__":
    main()
