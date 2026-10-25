"""Case domain: cases, versions, regulatory clocks and the transactional outbox.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-25

Integrity rules enforced here rather than in application code (see ADR 0009, ADR 0010):

- ``case_versions``: an approved version can never change, for any role. A draft may
  change only by bumping ``revision`` by exactly one, which makes lost updates
  impossible. At most one draft per case. Versions are never deleted.
- ``cases.day_zero`` can be set once and never moved.
- ``case_clocks`` is append-only: a started clock is never reset.
- ``outbox``: the app may only mark events as published (or record a failed attempt).
"""

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"


def _tenant_rls(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());
    """


def _append_only(table: str) -> str:
    return f"""
        CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
    """


def upgrade() -> None:
    # Which regulators' clock rules apply to the tenant's cases. Set by operators.
    op.execute("ALTER TABLE tenants ADD COLUMN regulators text[] NOT NULL DEFAULT '{FDA,EMA}'")

    op.execute(f"""
        CREATE TABLE cases (
            id             uuid PRIMARY KEY,
            tenant_id      uuid        NOT NULL REFERENCES tenants (id),
            seq            bigint      NOT NULL CHECK (seq >= 1),
            case_number    text        NOT NULL,
            region         text        NOT NULL,
            product_codes  text[]      NOT NULL DEFAULT '{{}}',
            day_zero       date,
            latest_version integer     NOT NULL DEFAULT 1 CHECK (latest_version >= 1),
            created_at     timestamptz NOT NULL,
            created_by     text        NOT NULL,
            UNIQUE (tenant_id, seq),
            UNIQUE (tenant_id, case_number),
            UNIQUE (tenant_id, id)
        );
        CREATE INDEX cases_scope_idx ON cases (tenant_id, region);
        CREATE INDEX cases_products_idx ON cases USING gin (product_codes);
        {_tenant_rls("cases")}

        CREATE FUNCTION guard_case() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'cases are never deleted' USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.id <> OLD.id OR NEW.tenant_id <> OLD.tenant_id OR NEW.seq <> OLD.seq
               OR NEW.case_number <> OLD.case_number THEN
                RAISE EXCEPTION 'case identity cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.day_zero IS NOT NULL AND NEW.day_zero IS DISTINCT FROM OLD.day_zero THEN
                RAISE EXCEPTION 'day 0 cannot be changed once set'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$;
        CREATE TRIGGER cases_guard BEFORE UPDATE OR DELETE ON cases
            FOR EACH ROW EXECUTE FUNCTION guard_case();
        CREATE TRIGGER cases_no_truncate BEFORE TRUNCATE ON cases
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

        GRANT SELECT, INSERT ON cases TO {APP_ROLE};
        GRANT UPDATE (region, product_codes, day_zero, latest_version) ON cases TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE case_versions (
            case_id        uuid        NOT NULL,
            version        integer     NOT NULL CHECK (version >= 1),
            tenant_id      uuid        NOT NULL,
            status         text        NOT NULL CHECK (status IN ('draft', 'approved')),
            schema_version integer     NOT NULL,
            content        jsonb       NOT NULL,
            content_hash   varchar(64) NOT NULL CHECK (content_hash ~ '^[0-9a-f]{{64}}$'),
            revision       integer     NOT NULL DEFAULT 0 CHECK (revision >= 0),
            created_at     timestamptz NOT NULL,
            created_by     text        NOT NULL,
            approved_at    timestamptz,
            approved_by    text,
            signature_id   uuid REFERENCES e_signatures (id),
            PRIMARY KEY (case_id, version),
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            CHECK ((status = 'approved') = (approved_at IS NOT NULL
                                            AND approved_by IS NOT NULL
                                            AND signature_id IS NOT NULL))
        );
        CREATE UNIQUE INDEX case_versions_one_draft ON case_versions (case_id)
            WHERE status = 'draft';
        {_tenant_rls("case_versions")}

        CREATE FUNCTION guard_case_version() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'case versions are never deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status = 'approved' THEN
                RAISE EXCEPTION 'approved case versions are immutable'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.case_id <> OLD.case_id OR NEW.version <> OLD.version
               OR NEW.tenant_id <> OLD.tenant_id OR NEW.created_at <> OLD.created_at
               OR NEW.created_by <> OLD.created_by THEN
                RAISE EXCEPTION 'case version identity cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF NEW.revision <> OLD.revision + 1 THEN
                RAISE EXCEPTION 'each change to a draft must increase revision by one'
                    USING ERRCODE = 'serialization_failure';
            END IF;
            RETURN NEW;
        END
        $$;
        CREATE TRIGGER case_versions_guard BEFORE UPDATE OR DELETE ON case_versions
            FOR EACH ROW EXECUTE FUNCTION guard_case_version();
        CREATE TRIGGER case_versions_no_truncate BEFORE TRUNCATE ON case_versions
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

        GRANT SELECT, INSERT, UPDATE ON case_versions TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE case_clocks (
            id               bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id        uuid        NOT NULL,
            case_id          uuid        NOT NULL,
            case_version     integer     NOT NULL,
            regulator        text        NOT NULL,
            rule_id          text        NOT NULL,
            rule_set_version text        NOT NULL,
            clock_start      date        NOT NULL,
            due_date         date        NOT NULL CHECK (due_date > clock_start),
            created_at       timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            FOREIGN KEY (case_id, case_version) REFERENCES case_versions (case_id, version),
            UNIQUE (case_id, case_version, regulator, rule_id)
        );
        CREATE INDEX case_clocks_due_idx ON case_clocks (tenant_id, due_date);
        {_tenant_rls("case_clocks")}
        {_append_only("case_clocks")}
        GRANT SELECT, INSERT ON case_clocks TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE outbox (
            id           uuid PRIMARY KEY,
            seq          bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
            tenant_id    uuid        NOT NULL REFERENCES tenants (id),
            topic        text        NOT NULL,
            event_type   text        NOT NULL,
            aggregate_id text        NOT NULL,
            payload      jsonb       NOT NULL,
            created_at   timestamptz NOT NULL,
            published_at timestamptz,
            attempts     integer     NOT NULL DEFAULT 0,
            last_error   text
        );
        CREATE INDEX outbox_pending_idx ON outbox (tenant_id, seq) WHERE published_at IS NULL;
        {_tenant_rls("outbox")}
        GRANT SELECT, INSERT ON outbox TO {APP_ROLE};
        GRANT UPDATE (published_at, attempts, last_error) ON outbox TO {APP_ROLE};
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    op.execute("DROP TABLE outbox")
    op.execute("DROP TABLE case_clocks")
    op.execute("DROP TABLE case_versions")
    op.execute("DROP FUNCTION guard_case_version()")
    op.execute("DROP TABLE cases")
    op.execute("DROP FUNCTION guard_case()")
    op.execute("ALTER TABLE tenants DROP COLUMN regulators")
