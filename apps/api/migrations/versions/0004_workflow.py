"""Workflow: case stage, human review tasks and deadline escalations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-25

- ``cases.workflow_stage`` mirrors where the Temporal case workflow is, for queues and
  screens. Temporal remains the source of truth for the workflow itself.
- ``review_tasks``: open tasks may be claimed, assigned and closed exactly once; a closed
  task never changes again. At most one open task per case and kind.
- ``deadline_escalations`` is append-only, unique per obligation and level, so a
  retried activity or a restarted worker never escalates twice.
"""

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"

STAGES = (
    "received", "pre_processing", "ai_processing", "triage", "held_for_follow_up",
    "data_entry_review", "quality_medical_review", "approval", "approved", "submitted",
    "follow_up",
)  # fmt: skip


def _rls(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());
    """


def upgrade() -> None:
    # Share of non-serious cases sampled for quality review. Set by operators.
    op.execute("""
        ALTER TABLE tenants ADD COLUMN qc_sample_rate numeric(4, 3) NOT NULL DEFAULT 0.1
            CHECK (qc_sample_rate BETWEEN 0 AND 1)
    """)

    stages = ", ".join(f"'{s}'" for s in STAGES)
    op.execute(f"""
        ALTER TABLE cases ADD COLUMN workflow_stage text NOT NULL DEFAULT 'received'
            CHECK (workflow_stage IN ({stages}));
        GRANT UPDATE (workflow_stage) ON cases TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE review_tasks (
            id            uuid PRIMARY KEY,
            tenant_id     uuid        NOT NULL,
            case_id       uuid        NOT NULL,
            case_version  integer     NOT NULL,
            kind          text        NOT NULL CHECK (kind IN
                              ('data_entry', 'quality_review', 'medical_review', 'approval')),
            status        text        NOT NULL DEFAULT 'open' CHECK (status IN
                              ('open', 'completed', 'returned', 'cancelled')),
            assignee      text,
            created_at    timestamptz NOT NULL,
            closed_at     timestamptz,
            closed_by     text,
            close_reason  text,
            signature_id  uuid REFERENCES e_signatures (id),
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            FOREIGN KEY (case_id, case_version) REFERENCES case_versions (case_id, version),
            CHECK ((status = 'open') = (closed_at IS NULL)),
            CHECK (status <> 'completed' OR signature_id IS NOT NULL)
        );
        CREATE UNIQUE INDEX review_tasks_one_open ON review_tasks (case_id, kind)
            WHERE status = 'open';
        CREATE INDEX review_tasks_queue_idx ON review_tasks (tenant_id, kind, status);
        {_rls("review_tasks")}

        CREATE FUNCTION guard_review_task() RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'review tasks are never deleted'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            IF OLD.status <> 'open' THEN
                RAISE EXCEPTION 'closed review tasks cannot change'
                    USING ERRCODE = 'insufficient_privilege';
            END IF;
            RETURN NEW;
        END
        $$;
        CREATE TRIGGER review_tasks_guard BEFORE UPDATE OR DELETE ON review_tasks
            FOR EACH ROW EXECUTE FUNCTION guard_review_task();
        CREATE TRIGGER review_tasks_no_truncate BEFORE TRUNCATE ON review_tasks
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();

        GRANT SELECT, INSERT ON review_tasks TO {APP_ROLE};
        GRANT UPDATE (status, assignee, closed_at, closed_by, close_reason, signature_id)
            ON review_tasks TO {APP_ROLE};
    """)

    op.execute(f"""
        CREATE TABLE deadline_escalations (
            id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
            tenant_id    uuid        NOT NULL,
            case_id      uuid        NOT NULL,
            case_version integer     NOT NULL,
            regulator    text        NOT NULL,
            rule_id      text        NOT NULL,
            due_date     date        NOT NULL,
            level        text        NOT NULL CHECK (level IN ('warning', 'escalation', 'overdue')),
            created_at   timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            UNIQUE (case_id, case_version, regulator, rule_id, level)
        );
        {_rls("deadline_escalations")}
        CREATE TRIGGER deadline_escalations_append_only
            BEFORE UPDATE OR DELETE ON deadline_escalations
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER deadline_escalations_no_truncate
            BEFORE TRUNCATE ON deadline_escalations
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
        GRANT SELECT, INSERT ON deadline_escalations TO {APP_ROLE};
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    op.execute("DROP TABLE deadline_escalations")
    op.execute("DROP TABLE review_tasks")
    op.execute("DROP FUNCTION guard_review_task()")
    op.execute("ALTER TABLE cases DROP COLUMN workflow_stage")
    op.execute("ALTER TABLE tenants DROP COLUMN qc_sample_rate")
