"""Review UI: duplicate merge, tenant self-administration.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-25

- ``cases.merged_into`` (with ``merged_at``, ``merged_by``): a duplicate merged into a
  master case. Set once, never changed or cleared (trigger). The workflow stage
  ``merged`` marks its end.
- E-signature meaning ``Duplicate merged``: the person who merges signs it.
- Tenants: a tenant admin may update their own tenant's settings (AI automation level,
  QC sampling rate, regulators, intake mailbox), and nothing else. Row-level security
  on ``tenants`` lets every tenant read the directory but update only its own row; the
  owner role (migrations, seeding) is not subject to it.
- ``ai_tenant_budgets``: writable by the tenant admin through the app role, under RLS.
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"

STAGES = (
    "received", "pre_processing", "ai_processing", "triage", "held_for_follow_up",
    "data_entry_review", "quality_medical_review", "approval", "approved", "submitted",
    "follow_up", "merged",
)  # fmt: skip
MEANINGS = ("Reviewed", "Quality reviewed", "Medically approved", "Approved", "Duplicate merged")


def upgrade() -> None:
    stages = ", ".join(f"'{s}'" for s in STAGES)
    meanings = ", ".join(f"'{m}'" for m in MEANINGS)
    op.execute(f"""
        ALTER TABLE cases DROP CONSTRAINT cases_workflow_stage_check;
        ALTER TABLE cases ADD CONSTRAINT cases_workflow_stage_check
            CHECK (workflow_stage IN ({stages}));

        ALTER TABLE e_signatures DROP CONSTRAINT e_signatures_meaning_check;
        ALTER TABLE e_signatures ADD CONSTRAINT e_signatures_meaning_check
            CHECK (meaning IN ({meanings}));

        ALTER TABLE cases
            ADD COLUMN merged_into uuid,
            ADD COLUMN merged_at   timestamptz,
            ADD COLUMN merged_by   text,
            ADD CONSTRAINT cases_merged_into_fk
                FOREIGN KEY (tenant_id, merged_into) REFERENCES cases (tenant_id, id),
            ADD CONSTRAINT cases_not_merged_into_itself CHECK (merged_into <> id),
            ADD CONSTRAINT cases_merge_complete
                CHECK ((merged_into IS NULL) = (merged_at IS NULL)
                       AND (merged_into IS NULL) = (merged_by IS NULL));
        GRANT UPDATE (merged_into, merged_at, merged_by) ON cases TO {APP_ROLE};

        CREATE FUNCTION guard_case_merge() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF OLD.merged_into IS NOT NULL AND (
                NEW.merged_into IS DISTINCT FROM OLD.merged_into
                OR NEW.merged_at IS DISTINCT FROM OLD.merged_at
                OR NEW.merged_by IS DISTINCT FROM OLD.merged_by
            ) THEN
                RAISE EXCEPTION 'a merge is final'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$;
        CREATE TRIGGER cases_merge_is_final BEFORE UPDATE ON cases
            FOR EACH ROW EXECUTE FUNCTION guard_case_merge();
    """)

    op.execute(f"""
        ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
        CREATE POLICY tenant_directory_read ON tenants FOR SELECT USING (true);
        CREATE POLICY tenant_admin_update ON tenants FOR UPDATE
            USING (id = app_current_tenant()) WITH CHECK (id = app_current_tenant());
        GRANT UPDATE (ai_automation_level, qc_sample_rate, regulators, intake_mailbox)
            ON tenants TO {APP_ROLE};

        GRANT INSERT, UPDATE ON ai_tenant_budgets TO {APP_ROLE};
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    op.execute(f"""
        REVOKE INSERT, UPDATE ON ai_tenant_budgets FROM {APP_ROLE};
        REVOKE UPDATE ON tenants FROM {APP_ROLE};
        DROP POLICY tenant_admin_update ON tenants;
        DROP POLICY tenant_directory_read ON tenants;
        ALTER TABLE tenants DISABLE ROW LEVEL SECURITY;
        DROP TRIGGER cases_merge_is_final ON cases;
        DROP FUNCTION guard_case_merge();
        ALTER TABLE cases DROP CONSTRAINT cases_merge_complete,
            DROP CONSTRAINT cases_not_merged_into_itself, DROP CONSTRAINT cases_merged_into_fk,
            DROP COLUMN merged_by, DROP COLUMN merged_at, DROP COLUMN merged_into;
    """)
