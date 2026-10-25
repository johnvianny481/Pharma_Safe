"""Email intake: the same email processed twice creates one case, sources stay exact."""

import imaplib
import secrets
import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from typing import Any
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import Engine, func, select, text

from pharmasafe_api.intake.email_intake import EMAIL_ACTOR, EmailPoller, parse_email
from pharmasafe_api.intake.models import IntakeReportRow, IntakeSubmissionRow, SourceDocumentRow
from pharmasafe_api.intake.service import IntakeDeps, ingest
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.blobstore import AzureBlobStore, InMemoryBlobStore
from pharmasafe_platform.db import Database
from pharmasafe_platform.settings import Settings


def _message(to: str = "intake@acme.pharmasafe.local", **headers: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = "Dr Silva <silva@example.org>"
    message["To"] = to
    message["Subject"] = "Suspected adverse reaction"
    message["Message-ID"] = f"<{secrets.token_hex(8)}@example.org>"
    for name, value in headers.items():
        del message[name]
        message[name] = value
    message.set_content("Patient JD, 45, developed a rash two days after starting Acmezol.")
    message.add_attachment(b"%PDF-1.4 \x00\x01\x02 scanned", maintype="application", subtype="pdf",
                           filename="scan.pdf")  # fmt: skip
    return message


def _counts(db: Database, tenant: UUID) -> tuple[int, int]:
    with db.tenant_session(tenant) as session:
        submissions = session.scalar(select(func.count()).select_from(IntakeSubmissionRow))
        reports = session.scalar(select(func.count()).select_from(IntakeReportRow))
    return submissions or 0, reports or 0


def test_the_same_email_processed_twice_creates_one_case(db: Database, tenant_a: UUID) -> None:
    deps = IntakeDeps(blobs=InMemoryBlobStore(), ocr=None, registry=seed_registry())
    raw = _message().as_bytes()
    with db.tenant_session(tenant_a) as session:
        first = ingest(session, tenant_a, EMAIL_ACTOR, parse_email(raw), deps)
    with db.tenant_session(tenant_a) as session:
        second = ingest(session, tenant_a, EMAIL_ACTOR, parse_email(raw), deps)
    assert first.created
    assert not second.created
    assert second.submission.id == first.submission.id
    assert second.case_ids == first.case_ids
    assert len(first.case_ids) == 1
    assert _counts(db, tenant_a) == (1, 1)


def test_redelivered_email_with_extra_headers_is_still_one_case(
    db: Database, tenant_a: UUID
) -> None:
    deps = IntakeDeps(blobs=InMemoryBlobStore(), ocr=None, registry=seed_registry())
    message = _message()
    raw = message.as_bytes()
    message["Received"] = "from relay.example.org by mx.example.org"
    with db.tenant_session(tenant_a) as session:
        ingest(session, tenant_a, EMAIL_ACTOR, parse_email(raw), deps)
        again = ingest(session, tenant_a, EMAIL_ACTOR, parse_email(message.as_bytes()), deps)
    assert not again.created
    assert _counts(db, tenant_a) == (1, 1)


def test_an_email_with_several_reports_becomes_several_cases(db: Database, tenant_a: UUID) -> None:
    deps = IntakeDeps(blobs=InMemoryBlobStore(), ocr=None, registry=seed_registry())
    message = EmailMessage()
    message["From"] = "safety@distributor.example"
    message["To"] = "intake@acme.pharmasafe.local"
    message["Message-ID"] = f"<{secrets.token_hex(8)}@example.org>"
    message.set_content(
        "Report 1\nPatient AB developed a rash.\n\nReport 2\nPatient CD had a headache.\n"
    )
    raw = message.as_bytes(policy=message.policy.clone(linesep="\r\n"))  # as sent over SMTP
    with db.tenant_session(tenant_a) as session:
        result = ingest(session, tenant_a, EMAIL_ACTOR, parse_email(raw), deps)
    assert len(result.case_ids) == 2


def test_email_sources_are_kept_exactly(
    db: Database, tenant_a: UUID, blob_store: AzureBlobStore
) -> None:
    deps = IntakeDeps(blobs=blob_store, ocr=None, registry=seed_registry())
    raw = _message().as_bytes()
    submission = parse_email(raw)
    with db.tenant_session(tenant_a) as session:
        result = ingest(session, tenant_a, EMAIL_ACTOR, submission, deps)
        documents = session.scalars(
            select(SourceDocumentRow)
            .where(SourceDocumentRow.submission_id == result.submission.id)
            .order_by(SourceDocumentRow.position)
        ).all()
    assert [d.part for d in documents] == ["message", "body", "attachment"]
    for row, original in zip(documents, submission.documents, strict=True):
        assert blob_store.get_verified(row.blob_name, row.sha256) == original.data
    assert blob_store.get_verified(documents[0].blob_name, documents[0].sha256) == raw


def test_poller_reads_each_mailbox_message_into_one_case(
    db: Database,
    owner_engine: Engine,
    make_tenant: Callable[[], UUID],
    mail_server: Any,
) -> None:
    tenant = make_tenant()
    mailbox = f"intake-{secrets.token_hex(4)}@pharmasafe.test"
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET intake_mailbox = :m WHERE id = :t"),
            {"m": mailbox, "t": tenant},
        )
    with smtplib.SMTP(mail_server.host, mail_server.smtp_port, timeout=10) as smtp:
        smtp.send_message(_message(to=mailbox))

    settings = Settings(
        imap_host=mail_server.host,
        imap_port=mail_server.imap_port,
        imap_ssl=False,
        imap_password=SecretStr("any"),
    )
    deps = IntakeDeps(blobs=InMemoryBlobStore(), ocr=None, registry=seed_registry())
    poller = EmailPoller(db, deps, settings)

    first = poller.poll_once()
    assert first.new_submissions == 1
    assert first.failures == 0
    assert _counts(db, tenant) == (1, 1)

    # Nothing new: the message was marked seen only after it was stored.
    poller.poll_once()
    assert _counts(db, tenant) == (1, 1)

    # Simulate a crash before \Seen was set: the message is read again, but stays one case.
    with imaplib.IMAP4(mail_server.host, mail_server.imap_port) as imap:
        imap.login(mailbox, "any")
        imap.select("INBOX")
        imap.store("1:*", "-FLAGS", "(\\Seen)")
    replay = poller.poll_once()
    assert replay.duplicates >= 1
    assert _counts(db, tenant) == (1, 1)
