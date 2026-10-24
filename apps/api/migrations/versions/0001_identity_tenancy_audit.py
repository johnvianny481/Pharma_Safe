"""Identity, tenancy, audit ledger and e-signatures.

Revision ID: 0001
Revises:
Create Date: 2026-10-25

Security model (see ADR 0005 and ADR 0006):

- ``pharmasafe_app`` is a NOLOGIN group role holding the application's privileges. The
  deployment grants it to the login role the services use. It is never the table owner
  and never has BYPASSRLS, so row-level security always applies to it.
- Every table with a ``tenant_id`` has RLS enabled *and forced* (forced also binds the
  owner if it is not a superuser), with a policy comparing ``tenant_id`` to
  ``app_current_tenant()``. With no tenant set, the function returns NULL and no rows match.
- ``audit_ledger`` and ``e_signatures`` are append-only: the app role gets SELECT and
  INSERT only, and triggers reject UPDATE, DELETE and TRUNCATE for every role.
"""

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'pharmasafe_app') THEN
                CREATE ROLE pharmasafe_app NOLOGIN;
            END IF;
        END $$
    """)

    op.execute("""
        CREATE FUNCTION app_current_tenant() RETURNS uuid
        LANGUAGE sql STABLE PARALLEL SAFE
        AS $$ SELECT nullif(current_setting('app.tenant_id', true), '')::uuid $$
    """)

    op.execute("""
        CREATE FUNCTION reject_modification() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'insufficient_privilege';
        END
        $$
    """)

    # Tenant directory: no case or personal data, so it is readable (not writable) by the
    # app without a tenant context. Operators create tenants with the owner role.
    op.execute(f"""
        CREATE TABLE tenants (
            id         uuid PRIMARY KEY,
            slug       varchar(64)  NOT NULL UNIQUE CHECK (slug ~ '^[a-z0-9-]+$'),
            name       varchar(200) NOT NULL,
            region     varchar(8)   NOT NULL CHECK (region IN ('eu', 'us', 'in')),
            created_at timestamptz  NOT NULL DEFAULT now()
        );
        GRANT SELECT ON tenants TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE audit_ledger (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id    uuid        NOT NULL REFERENCES tenants (id),
            seq          bigint      NOT NULL CHECK (seq >= 1),
            occurred_at  timestamptz NOT NULL,
            actor_id     text        NOT NULL,
            actor_name   text        NOT NULL,
            action       text        NOT NULL,
            entity_type  text        NOT NULL,
            entity_id    text        NOT NULL,
            case_version integer,
            field        text,
            old_value    jsonb,
            new_value    jsonb,
            reason       text,
            prev_hash    varchar(64) NOT NULL CHECK (prev_hash ~ '^[0-9a-f]{{64}}$'),
            hash         varchar(64) NOT NULL UNIQUE CHECK (hash ~ '^[0-9a-f]{{64}}$'),
            UNIQUE (tenant_id, seq)
        );
        CREATE INDEX audit_ledger_entity_idx ON audit_ledger (tenant_id, entity_type, entity_id);

        ALTER TABLE audit_ledger ENABLE ROW LEVEL SECURITY;
        ALTER TABLE audit_ledger FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON audit_ledger
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());

        CREATE TRIGGER audit_ledger_append_only
            BEFORE UPDATE OR DELETE ON audit_ledger
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER audit_ledger_no_truncate
            BEFORE TRUNCATE ON audit_ledger
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

        GRANT SELECT, INSERT ON audit_ledger TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE e_signatures (
            id              uuid PRIMARY KEY,
            tenant_id       uuid        NOT NULL REFERENCES tenants (id),
            entity_type     text        NOT NULL,
            entity_id       text        NOT NULL,
            entity_version  integer     NOT NULL CHECK (entity_version >= 1),
            content_hash    varchar(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{{64}}$'),
            meaning         text        NOT NULL CHECK (meaning IN
                                ('Reviewed', 'Quality reviewed', 'Medically approved', 'Approved')),
            reason          text,
            signer_id       text        NOT NULL,
            signer_name     text        NOT NULL,
            signed_at       timestamptz NOT NULL,
            auth_time       timestamptz NOT NULL,
            reauth_token_id text        NOT NULL UNIQUE,
            signature_hash  varchar(64) NOT NULL CHECK (signature_hash ~ '^[0-9a-f]{{64}}$')
        );
        CREATE INDEX e_signatures_entity_idx
            ON e_signatures (tenant_id, entity_type, entity_id, entity_version);

        ALTER TABLE e_signatures ENABLE ROW LEVEL SECURITY;
        ALTER TABLE e_signatures FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON e_signatures
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());

        CREATE TRIGGER e_signatures_append_only
            BEFORE UPDATE OR DELETE ON e_signatures
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER e_signatures_no_truncate
            BEFORE TRUNCATE ON e_signatures
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

        GRANT SELECT, INSERT ON e_signatures TO {APP_ROLE};
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    op.execute("DROP TABLE e_signatures")
    op.execute("DROP TABLE audit_ledger")
    op.execute("DROP TABLE tenants")
    op.execute("DROP FUNCTION reject_modification()")
    op.execute("DROP FUNCTION app_current_tenant()")
