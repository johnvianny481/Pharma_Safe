"""Email intake: parse a raw message into source documents, and poll mailboxes over IMAP.

The raw message is stored first, exactly as received (it is the evidence). Its body and
each attachment are stored too, as decoded bytes, so they can be read and processed on
their own.

**Exactly one case per email.** The submission key is the ``Message-ID`` (or, without
one, the SHA-256 of the raw message). The poller fetches unseen messages without marking
them, ingests each in its own transaction, and only then flags it ``\\Seen``. If it dies
in between, the next poll reads the message again, and the key makes that a no-op.

Run with ``python -m pharmasafe_api.intake.email_intake`` (``--once`` for a single pass).
"""

import argparse
import imaplib
import logging
import time
from dataclasses import dataclass
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from uuid import UUID

from sqlalchemy import select

from pharmasafe_api.intake.extraction import TesseractOcr
from pharmasafe_api.intake.service import (
    Channel,
    IncomingDocument,
    IngestResult,
    IntakeDeps,
    Submission,
    ingest,
)
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.blobstore import AzureBlobStore, sha256_hex
from pharmasafe_platform.db import Database
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.models import TenantRow
from pharmasafe_platform.settings import Settings, get_settings

log = logging.getLogger(__name__)

EMAIL_ACTOR = Actor(id="system:email-intake", name="Email intake")
MAX_HEADER_LENGTH = 300


def parse_email(raw: bytes) -> Submission:
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not isinstance(message, EmailMessage):  # policy.default always gives EmailMessage
        raise TypeError("unexpected email message class")
    documents = [
        IncomingDocument(
            part="message", content_type="message/rfc822", data=raw, filename="message.eml"
        )
    ]

    body = message.get_body(preferencelist=("plain", "html"))
    if isinstance(body, EmailMessage):
        documents.append(
            IncomingDocument(part="body", content_type=_content_type(body), data=_payload(body))
        )
    for attachment in message.iter_attachments():
        if isinstance(attachment, EmailMessage):
            documents.append(
                IncomingDocument(
                    part="attachment",
                    content_type=_content_type(attachment),
                    data=_payload(attachment),
                    filename=attachment.get_filename(),
                )
            )

    message_id = str(message.get("Message-ID", "")).strip()
    external_id = message_id[:MAX_HEADER_LENGTH] if message_id else f"sha256:{sha256_hex(raw)}"
    return Submission(
        channel=Channel.EMAIL,
        external_id=external_id,
        request_hash=sha256_hex(raw),
        documents=documents,
        sender=_header(message, "From"),
        subject=_header(message, "Subject"),
    )


def _content_type(part: EmailMessage) -> str:
    content_type = part.get_content_type()
    charset = part.get_content_charset()
    return (
        f"{content_type}; charset={charset}"
        if charset and content_type.startswith("text/")
        else content_type
    )


def _payload(part: EmailMessage) -> bytes:
    data = part.get_payload(decode=True)
    if isinstance(data, bytes):
        return data
    # Attached messages (message/rfc822) have no single decoded payload: keep them whole.
    return part.as_bytes()


def _header(message: EmailMessage, name: str) -> str | None:
    value = message.get(name)
    return str(value)[:MAX_HEADER_LENGTH] if value is not None else None


# ---------------------------------------------------------------- polling


@dataclass
class PollResult:
    messages: int = 0
    new_submissions: int = 0
    duplicates: int = 0
    failures: int = 0


class EmailPoller:
    def __init__(self, database: Database, deps: IntakeDeps, settings: Settings) -> None:
        self._database = database
        self._deps = deps
        self._settings = settings

    def poll_once(self) -> PollResult:
        result = PollResult()
        with self._database.system_session() as session:
            mailboxes = session.execute(
                select(TenantRow.id, TenantRow.intake_mailbox)
                .where(TenantRow.intake_mailbox.is_not(None))
                .order_by(TenantRow.id)
            ).all()
        for tenant_id, mailbox in mailboxes:
            if mailbox:
                self._poll_mailbox(tenant_id, mailbox, result)
        return result

    def _poll_mailbox(self, tenant_id: UUID, mailbox: str, result: PollResult) -> None:
        imap_class = imaplib.IMAP4_SSL if self._settings.imap_ssl else imaplib.IMAP4
        with imap_class(self._settings.imap_host, self._settings.imap_port) as imap:
            imap.login(mailbox, self._settings.imap_password.get_secret_value())
            imap.select("INBOX")
            _, data = imap.uid("SEARCH", "UNSEEN")
            uids = data[0].decode().split() if data and data[0] else []
            for uid in uids:
                result.messages += 1
                try:
                    raw = _fetch(imap, uid)
                    outcome = self._ingest(tenant_id, raw)
                except Exception:  # one bad message must not stop the mailbox
                    result.failures += 1
                    log.exception("email intake failed", extra={"tenant_id": str(tenant_id)})
                    continue
                if outcome.created:
                    result.new_submissions += 1
                else:
                    result.duplicates += 1
                # Only after the submission committed: a crash before this line re-reads it.
                imap.uid("STORE", uid, "+FLAGS", "(\\Seen)")

    def _ingest(self, tenant_id: UUID, raw: bytes) -> IngestResult:
        with self._database.tenant_session(tenant_id) as session:
            return ingest(session, tenant_id, EMAIL_ACTOR, parse_email(raw), self._deps)


def _fetch(imap: imaplib.IMAP4, uid: str) -> bytes:
    # BODY.PEEK does not set \Seen; the poller sets it once the message is safely stored.
    status, data = imap.uid("FETCH", uid, "(BODY.PEEK[])")
    for item in data if status == "OK" else []:
        if isinstance(item, tuple):
            return bytes(item[1])
    raise imaplib.IMAP4.error(f"could not fetch message {uid}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll tenant intake mailboxes")
    parser.add_argument("--once", action="store_true", help="poll once and exit")
    parser.add_argument("--interval", type=float, default=30.0, help="seconds between polls")
    args = parser.parse_args()

    settings = get_settings()
    configure_logging("email-intake", settings.log_level)
    blobs = AzureBlobStore(
        settings.blob_endpoint,
        settings.blob_account,
        settings.blob_key.get_secret_value(),
        settings.blob_container,
    )
    deps = IntakeDeps(
        blobs=blobs, ocr=TesseractOcr.find(settings.tesseract_cmd), registry=seed_registry()
    )
    poller = EmailPoller(Database(settings.database_url), deps, settings)
    while True:
        result = poller.poll_once()
        log.info(
            "email poll finished", extra={"messages": result.messages, "failures": result.failures}
        )
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
