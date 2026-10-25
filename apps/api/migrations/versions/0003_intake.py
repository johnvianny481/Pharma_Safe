"""Intake: submissions, source documents, extracted text, and reports split into cases.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-25

Every intake table is append-only (see ADR 0012). A submission is unique per tenant,
channel and external id: the ``Idempotency-Key`` for API channels and the ``Message-ID``
for email. That uniqueness makes a repeated request or a re-read email a no-op.
"""

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

APP_ROLE = "pharmasafe_app"


def _protect(table: str) -> str:
    return f"""
        ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;
        ALTER TABLE {table} FORCE ROW LEVEL SECURITY;
        CREATE POLICY tenant_isolation ON {table}
            USING (tenant_id = app_current_tenant())
            WITH CHECK (tenant_id = app_current_tenant());
        CREATE TRIGGER {table}_append_only
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION reject_modification();
        CREATE TRIGGER {table}_no_truncate
            BEFORE TRUNCATE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION reject_modification();
        GRANT SELECT, INSERT ON {table} TO {APP_ROLE};
    """


def upgrade() -> None:
    # The tenant's monitored intake mailbox. Set by operators.
    op.execute("ALTER TABLE tenants ADD COLUMN intake_mailbox text UNIQUE")

    op.execute(f"""
        CREATE TABLE intake_submissions (
            id           uuid PRIMARY KEY,
            tenant_id    uuid        NOT NULL REFERENCES tenants (id),
            channel      text        NOT NULL CHECK (channel IN ('api', 'form', 'upload', 'email')),
            external_id  text        NOT NULL CHECK (length(external_id) BETWEEN 1 AND 300),
            request_hash varchar(64) NOT NULL CHECK (request_hash ~ '^[0-9a-f]{{64}}$'),
            received_at  timestamptz NOT NULL,
            received_by  text        NOT NULL,
            sender       text,
            subject      text,
            UNIQUE (tenant_id, channel, external_id),
            UNIQUE (tenant_id, id)
        );
        {_protect("intake_submissions")}
    """)

    op.execute(f"""
        CREATE TABLE source_documents (
            id            uuid PRIMARY KEY,
            tenant_id     uuid        NOT NULL,
            submission_id uuid        NOT NULL,
            position      integer     NOT NULL CHECK (position >= 0),
            part          text        NOT NULL CHECK (part IN
                              ('message', 'body', 'attachment', 'upload', 'text', 'form')),
            filename      text,
            content_type  text        NOT NULL,
            size          bigint      NOT NULL CHECK (size >= 0),
            sha256        varchar(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
            blob_name     text        NOT NULL,
            created_at    timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, submission_id) REFERENCES intake_submissions (tenant_id, id),
            UNIQUE (submission_id, position),
            UNIQUE (tenant_id, id)
        );
        {_protect("source_documents")}
    """)

    op.execute(f"""
        CREATE TABLE document_texts (
            document_id         uuid PRIMARY KEY,
            tenant_id           uuid        NOT NULL,
            method              text        NOT NULL,
            status              text        NOT NULL CHECK (status IN
                                    ('extracted', 'ocr_unavailable', 'unsupported', 'failed')),
            text                text        NOT NULL,
            language            varchar(8)  NOT NULL,
            language_confidence real        NOT NULL,
            created_at          timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, document_id) REFERENCES source_documents (tenant_id, id)
        );
        {_protect("document_texts")}
    """)

    op.execute(f"""
        CREATE TABLE intake_reports (
            id            uuid PRIMARY KEY,
            tenant_id     uuid        NOT NULL,
            submission_id uuid        NOT NULL,
            report_index  integer     NOT NULL CHECK (report_index >= 0),
            case_id       uuid        NOT NULL,
            document_id   uuid,
            text_start    integer,
            text_end      integer,
            language      varchar(8)  NOT NULL,
            created_at    timestamptz NOT NULL,
            FOREIGN KEY (tenant_id, submission_id) REFERENCES intake_submissions (tenant_id, id),
            FOREIGN KEY (tenant_id, case_id) REFERENCES cases (tenant_id, id),
            FOREIGN KEY (tenant_id, document_id) REFERENCES source_documents (tenant_id, id),
            CHECK ((document_id IS NULL AND text_start IS NULL AND text_end IS NULL)
                   OR (document_id IS NOT NULL AND 0 <= text_start AND text_start < text_end)),
            UNIQUE (submission_id, report_index)
        );
        CREATE INDEX intake_reports_case_idx ON intake_reports (tenant_id, case_id);
        {_protect("intake_reports")}
    """)


def downgrade() -> None:
    # Development only. A validated environment never downgrades past regulated records.
    op.execute("DROP TABLE intake_reports")
    op.execute("DROP TABLE document_texts")
    op.execute("DROP TABLE source_documents")
    op.execute("DROP TABLE intake_submissions")
    op.execute("ALTER TABLE tenants DROP COLUMN intake_mailbox")
