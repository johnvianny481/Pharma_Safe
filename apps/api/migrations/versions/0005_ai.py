"""AI gateway and pipeline: call ledger, budgets, AI runs, redactions, case vectors.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-25

- ``tenants.ai_automation_level``: L0, L1 (default) or L2. Serious cases are capped at L1
  by the pipeline, whatever this says.
- ``ai_calls`` (gateway): one row per model call or refusal; append-only.
- ``ai_tenant_budgets`` (gateway): monthly limit per tenant, set by operators.
- ``case_ai_runs``: what the pipeline did for a case version (bundle version, effective
  automation level, proposals, rejections, both seriousness signals, duplicates, coding,
  follow-up questions, narrative draft). Append-only; one row per case version and kind,
  so a retried activity cannot record twice.
- ``case_redactions``: the token mapping per case version and document, encrypted with
  the tenant's key. Append-only.
- ``case_vectors``: blocking keys and the similarity vector of each case's latest
  version, for duplicate detection. Derived data, so it is updated in place.
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"


def _rls(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());
    """


def _append_only(table: str) -> str:
    return f"""
        CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER {table}_no_truncate BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
        GRANT SELECT, INSERT ON {table} TO {APP_ROLE};
    """


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("""
        ALTER TABLE tenants ADD COLUMN ai_automation_level text NOT NULL DEFAULT 'L1'
            CHECK (ai_automation_level IN ('L0', 'L1', 'L2'))
    """)

    op.execute(f"""
        CREATE TABLE ai_calls (
            id             uuid PRIMARY KEY,
            tenant_id      uuid          NOT NULL REFERENCES tenants (id),
            case_id        text          NOT NULL,
            step           text          NOT NULL,
            bundle_version text          NOT NULL,
            prompt_version text          NOT NULL,
            provider       text,
            model          text,
            attempts       integer       NOT NULL,
            fallback_used  boolean       NOT NULL,
            outcome        text          NOT NULL,
            input_tokens   integer       NOT NULL,
            output_tokens  integer       NOT NULL,
            cost_usd       numeric(12, 6) NOT NULL,
            latency_ms     integer       NOT NULL,
            created_at     timestamptz   NOT NULL
        );
        CREATE INDEX ai_calls_tenant_month_idx ON ai_calls (tenant_id, created_at);
        {_rls("ai_calls")}
        {_append_only("ai_calls")}

        CREATE TABLE ai_tenant_budgets (
            tenant_id         uuid PRIMARY KEY REFERENCES tenants (id),
            monthly_limit_usd numeric(12, 2) NOT NULL CHECK (monthly_limit_usd >= 0)
        );
        {_rls("ai_tenant_budgets")}
        GRANT SELECT ON ai_tenant_budgets TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE case_ai_runs (
            id                 uuid PRIMARY KEY,
            tenant_id          uuid        NOT NULL,
            case_id            uuid        NOT NULL,
            case_version       integer     NOT NULL,
            kind               text        NOT NULL CHECK (kind IN ('pipeline', 'narrative')),
            bundle_version     text        NOT NULL,
            status             text        NOT NULL
                CHECK (status IN ('completed', 'manual_fallback')),
            automation_level   text        NOT NULL CHECK (automation_level IN ('L0', 'L1', 'L2')),
            flags              text[]      NOT NULL DEFAULT '{{}}',
            result             jsonb       NOT NULL,
            created_at         timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            FOREIGN KEY (case_id, case_version) REFERENCES case_versions (case_id, version),
            UNIQUE (case_id, case_version, kind)
        );
        {_rls("case_ai_runs")}
        {_append_only("case_ai_runs")}

        CREATE TABLE case_redactions (
            id           uuid PRIMARY KEY,
            tenant_id    uuid        NOT NULL,
            case_id      uuid        NOT NULL,
            case_version integer     NOT NULL,
            document_id  text        NOT NULL,
            key_id       text        NOT NULL,
            ciphertext   bytea       NOT NULL,
            created_at   timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            UNIQUE (case_id, case_version, document_id)
        );
        {_rls("case_redactions")}
        {_append_only("case_redactions")}

        CREATE TABLE case_vectors (
            case_id      uuid PRIMARY KEY,
            tenant_id    uuid        NOT NULL,
            case_version integer     NOT NULL,
            drugs        text[]      NOT NULL,
            reactions    text[]      NOT NULL,
            keys         jsonb       NOT NULL,
            embedding    vector      NOT NULL,
            updated_at   timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id)
        );
        CREATE INDEX case_vectors_drugs_idx ON case_vectors USING gin (drugs);
        CREATE INDEX case_vectors_reactions_idx ON case_vectors USING gin (reactions);
        {_rls("case_vectors")}
        GRANT SELECT, INSERT, UPDATE ON case_vectors TO {APP_ROLE};
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    for table in ("case_vectors", "case_redactions", "case_ai_runs", "ai_tenant_budgets",
                  "ai_calls"):  # fmt: skip
        op.execute(f"DROP TABLE {table}")
    op.execute("ALTER TABLE tenants DROP COLUMN ai_automation_level")
