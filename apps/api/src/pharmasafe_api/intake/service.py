"""Intake: receive a submission once, keep its sources, and turn it into cases.

Every channel (REST, web form, upload, email) ends in :func:`ingest`, which:

1. **Claims the submission** by ``(tenant, channel, external_id)``. The external id is
   the ``Idempotency-Key`` for API channels and the ``Message-ID`` for email. A second
   arrival returns the first submission and creates nothing, so a retried request or a
   re-read email never makes a second case.
2. **Stores every source document** byte for byte in immutable blob storage, named by
   its SHA-256, and records the hash. The ``intake.received`` audit entry lists the
   hashes, so the hash chain also protects the sources.
3. **Pre-processes**: text extraction (OCR for images), language detection, and splitting
   a document that holds several reports.
4. **Creates one case per report**, linked to the document and character span it came
   from. The receipt date is recorded as a system value (source ``import``) that a
   person must confirm before approval.

Steps 3 and 4 run synchronously here. Phase 4 moves them into the Temporal workflow,
started from the ``intake.received`` outbox event.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.intake.extraction import ExtractedText, OcrEngine, TextStatus, extract_text
from pharmasafe_api.intake.language import UNDETERMINED, detect_language
from pharmasafe_api.intake.models import (
    DocumentTextRow,
    IntakeReportRow,
    IntakeSubmissionRow,
    SourceDocumentRow,
)
from pharmasafe_api.intake.splitting import Span, split_reports
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Source, Tracked
from pharmasafe_domain.rules.clocks import RuleSetRegistry
from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.auth import DataScope
from pharmasafe_platform.blobstore import BlobStore, sha256_hex
from pharmasafe_platform.hashing import canonical_json
from pharmasafe_platform.outbox import OutboxEvent, enqueue

ENTITY = "intake_submission"


class Channel(StrEnum):
    API = "api"
    FORM = "form"
    UPLOAD = "upload"
    EMAIL = "email"


class IdempotencyConflict(Exception):
    """The idempotency key was already used for a different request."""


class SubmissionNotFound(Exception):
    pass


@dataclass(frozen=True)
class IncomingDocument:
    part: str  # message, body, attachment, upload, text, form
    content_type: str
    data: bytes
    filename: str | None = None
    # Text already known (form submissions); skips extraction.
    text: str | None = None


@dataclass(frozen=True)
class Submission:
    channel: Channel
    external_id: str
    request_hash: str
    documents: Sequence[IncomingDocument]
    sender: str | None = None
    subject: str | None = None
    # Structured case content entered on a form; other channels start empty.
    case_content: SafetyCase | None = None


@dataclass(frozen=True)
class IntakeDeps:
    blobs: BlobStore
    ocr: OcrEngine | None
    registry: RuleSetRegistry


@dataclass
class IngestResult:
    submission: IntakeSubmissionRow
    created: bool
    case_ids: list[UUID] = field(default_factory=list)


def request_hash(value: Any) -> str:
    """Hash of a request's content, to tell a retry from a reused idempotency key."""
    return sha256_hex(canonical_json(value).encode("utf-8"))


def ingest(
    session: Session,
    tenant_id: UUID,
    actor: Actor,
    submission: Submission,
    deps: IntakeDeps,
    *,
    now: datetime | None = None,
) -> IngestResult:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    # A concurrent duplicate blocks on the unique key here until the first one commits,
    # then sees it and returns it.
    claimed = session.execute(
        insert(IntakeSubmissionRow)
        .values(
            id=uuid4(),
            tenant_id=tenant_id,
            channel=submission.channel.value,
            external_id=submission.external_id,
            request_hash=submission.request_hash,
            received_at=now,
            received_by=actor.id,
            sender=submission.sender,
            subject=submission.subject,
        )
        .on_conflict_do_nothing(index_elements=["tenant_id", "channel", "external_id"])
        .returning(IntakeSubmissionRow.id)
    ).scalar_one_or_none()

    if claimed is None:
        return _existing(session, tenant_id, actor, submission, now)

    row = session.get_one(IntakeSubmissionRow, claimed)
    texts = _store_documents(session, row, submission.documents, deps, now)
    case_ids = _create_cases(session, row, actor, submission, texts, deps, now)
    _audit(
        session,
        row,
        actor,
        now,
        "intake.received",
        {
            "channel": row.channel,
            "documents": [
                {"id": str(d.id), "part": d.part, "sha256": d.sha256, "size": d.size}
                for d, _ in texts
            ],
            "case_ids": [str(c) for c in case_ids],
        },
    )
    enqueue(
        session,
        tenant_id,
        OutboxEvent(
            topic="intake",
            event_type="intake.received",
            aggregate_id=str(row.id),
            payload={
                "submission_id": str(row.id),
                "channel": row.channel,
                "case_ids": [str(c) for c in case_ids],
            },
        ),
        now=now,
    )
    return IngestResult(submission=row, created=True, case_ids=case_ids)


def _existing(
    session: Session, tenant_id: UUID, actor: Actor, submission: Submission, now: datetime
) -> IngestResult:
    row = session.scalars(
        select(IntakeSubmissionRow).where(
            IntakeSubmissionRow.channel == submission.channel.value,
            IntakeSubmissionRow.external_id == submission.external_id,
        )
    ).one()
    if row.request_hash != submission.request_hash:
        if submission.channel is not Channel.EMAIL:
            raise IdempotencyConflict("this Idempotency-Key was used for a different request")
        # The same Message-ID with different bytes: usually re-delivery with extra headers.
        # Keep the first message, and record that another one arrived.
        _audit(
            session,
            row,
            actor,
            now,
            "intake.duplicate_ignored",
            {"request_hash": submission.request_hash},
        )
    return IngestResult(submission=row, created=False, case_ids=list(case_ids_of(session, row.id)))


@dataclass(frozen=True)
class _DocText:
    extracted: ExtractedText | None
    language: str
    confidence: float


def _store_documents(
    session: Session,
    submission: IntakeSubmissionRow,
    documents: Sequence[IncomingDocument],
    deps: IntakeDeps,
    now: datetime,
) -> list[tuple[SourceDocumentRow, _DocText]]:
    stored = []
    for position, doc in enumerate(documents):
        digest = sha256_hex(doc.data)
        # Content-addressed per tenant: the same bytes are stored once, never replaced.
        blob = deps.blobs.put_immutable(
            f"{submission.tenant_id}/{digest}", doc.data, doc.content_type
        )
        row = SourceDocumentRow(
            id=uuid4(),
            tenant_id=submission.tenant_id,
            submission_id=submission.id,
            position=position,
            part=doc.part,
            filename=doc.filename,
            content_type=doc.content_type,
            size=blob.size,
            sha256=blob.sha256,
            blob_name=blob.name,
            created_at=now,
        )
        session.add(row)
        session.flush()
        text = _text_of(doc, deps)
        if text.extracted is not None:
            session.add(
                DocumentTextRow(
                    document_id=row.id,
                    tenant_id=row.tenant_id,
                    method=text.extracted.method,
                    status=text.extracted.status.value,
                    text=text.extracted.text,
                    language=text.language,
                    language_confidence=text.confidence,
                    created_at=now,
                )
            )
        stored.append((row, text))
    session.flush()
    return stored


def _text_of(doc: IncomingDocument, deps: IntakeDeps) -> _DocText:
    if doc.part == "message":
        # The raw email is kept as evidence; its parts are stored and read separately.
        return _DocText(None, UNDETERMINED, 0.0)
    if doc.text is not None:
        extracted = ExtractedText(doc.part, TextStatus.EXTRACTED, doc.text)
    else:
        extracted = extract_text(doc.data, doc.content_type, deps.ocr)
    detection = detect_language(extracted.text)
    return _DocText(extracted, detection.language, detection.confidence)


@dataclass(frozen=True)
class _Report:
    document: SourceDocumentRow | None
    span: Span | None
    text: str


def _plan_reports(texts: list[tuple[SourceDocumentRow, _DocText]]) -> list[_Report]:
    """One report per split section if any document holds several; otherwise one report.

    With a multi-report document, other documents (such as a cover email) stay attached
    to the submission but start no case of their own.
    """
    with_text = [(doc, t.extracted.text) for doc, t in texts if t.extracted and t.extracted.text]
    split = [(doc, text, split_reports(text)) for doc, text in with_text]
    multi = [
        _Report(doc, span, text[span.start : span.end])
        for doc, text, spans in split
        if len(spans) >= 2
        for span in spans
    ]
    if multi:
        return multi
    if not with_text:
        return [_Report(None, None, "")]
    # A single report: point it at the document with the most text.
    doc, text, spans = max(split, key=lambda item: len(item[1].strip()))
    span = spans[0] if spans else None
    return [_Report(doc if span else None, span, text[span.start : span.end] if span else "")]


def _create_cases(
    session: Session,
    submission: IntakeSubmissionRow,
    actor: Actor,
    incoming: Submission,
    texts: list[tuple[SourceDocumentRow, _DocText]],
    deps: IntakeDeps,
    now: datetime,
) -> list[UUID]:
    ctx = cases.CaseContext(registry=deps.registry, now=now)
    content = _with_receipt_date(incoming.case_content or SafetyCase(), now.date())
    case_ids = []
    for index, report in enumerate(_plan_reports(texts)):
        case, _ = cases.create_case(
            session,
            submission.tenant_id,
            actor,
            content,
            ctx,
            reason=f"intake via {submission.channel}",
        )
        session.add(
            IntakeReportRow(
                id=uuid4(),
                tenant_id=submission.tenant_id,
                submission_id=submission.id,
                report_index=index,
                case_id=case.id,
                document_id=report.document.id if report.document else None,
                text_start=report.span.start if report.span else None,
                text_end=report.span.end if report.span else None,
                language=detect_language(report.text).language,
                created_at=now,
            )
        )
        case_ids.append(case.id)
    session.flush()
    return case_ids


def _with_receipt_date(content: SafetyCase, received: date) -> SafetyCase:
    receipt = Tracked[date](value=received, source=Source.IMPORT)
    admin = content.admin.model_copy(
        update={
            "initial_receipt_date": content.admin.initial_receipt_date or receipt,
            "latest_receipt_date": content.admin.latest_receipt_date or receipt,
        }
    )
    return SafetyCase.from_json(content.model_copy(update={"admin": admin}).to_json())


def _audit(
    session: Session,
    submission: IntakeSubmissionRow,
    actor: Actor,
    now: datetime,
    action: str,
    value: dict[str, Any],
) -> None:
    append_audit(
        session,
        submission.tenant_id,
        AuditEvent(
            actor=actor,
            action=action,
            entity_type=ENTITY,
            entity_id=str(submission.id),
            new_value=value,
        ),
        now=now,
    )


# ---------------------------------------------------------------- reading


def case_ids_of(session: Session, submission_id: UUID) -> Sequence[UUID]:
    return session.scalars(
        select(IntakeReportRow.case_id)
        .where(IntakeReportRow.submission_id == submission_id)
        .order_by(IntakeReportRow.report_index)
    ).all()


def visible_submission(
    session: Session, submission_id: UUID, scope: DataScope
) -> IntakeSubmissionRow:
    """A submission the caller may see: unrestricted scope, or a linked case in scope.

    Sources hold everything a report said, so they are no more visible than its cases.
    """
    row = session.get(IntakeSubmissionRow, submission_id)
    if row is None:
        raise SubmissionNotFound(str(submission_id))
    if scope.products is None and scope.regions is None:
        return row
    linked = select(IntakeReportRow.case_id).where(IntakeReportRow.submission_id == row.id)
    visible = session.scalar(
        select(CaseRow.id).where(CaseRow.id.in_(linked), cases.scope_filter(scope)).limit(1)
    )
    if visible is None:
        raise SubmissionNotFound(str(submission_id))
    return row


def documents_of(session: Session, submission_id: UUID) -> Sequence[SourceDocumentRow]:
    return session.scalars(
        select(SourceDocumentRow)
        .where(SourceDocumentRow.submission_id == submission_id)
        .order_by(SourceDocumentRow.position)
    ).all()


@dataclass(frozen=True)
class CaseSource:
    """The report text a case came from: a span of one source document's text."""

    document_id: UUID
    text: str
    # Where ``text`` starts in the document text, so evidence offsets can point there.
    offset: int


def source_of_case(session: Session, case_id: UUID) -> CaseSource | None:
    """The case's report text, or None if it came with no readable text."""
    report = session.scalars(
        select(IntakeReportRow).where(IntakeReportRow.case_id == case_id)
    ).first()
    if report is None or report.document_id is None or report.text_start is None:
        return None
    document = session.get(DocumentTextRow, report.document_id)
    if document is None or report.text_end is None:
        return None
    text = document.text[report.text_start : report.text_end]
    return CaseSource(report.document_id, text, report.text_start) if text.strip() else None


def texts_of(session: Session, document_ids: Sequence[UUID]) -> dict[UUID, DocumentTextRow]:
    rows = session.scalars(
        select(DocumentTextRow).where(DocumentTextRow.document_id.in_(document_ids))
    )
    return {row.document_id: row for row in rows}


def reports_of(session: Session, submission_id: UUID) -> Sequence[tuple[IntakeReportRow, str]]:
    return session.execute(
        select(IntakeReportRow, CaseRow.case_number)
        .join(CaseRow, CaseRow.id == IntakeReportRow.case_id)
        .where(IntakeReportRow.submission_id == submission_id)
        .order_by(IntakeReportRow.report_index)
    ).all()


def document_content(
    session: Session, document_id: UUID, scope: DataScope, blobs: BlobStore
) -> tuple[SourceDocumentRow, bytes]:
    """The original bytes, checked against the SHA-256 recorded at intake."""
    row = session.get(SourceDocumentRow, document_id)
    if row is None:
        raise SubmissionNotFound(str(document_id))
    visible_submission(session, row.submission_id, scope)
    return row, blobs.get_verified(row.blob_name, row.sha256)


def form_document(form: dict[str, Any]) -> IncomingDocument:
    """A web-form submission kept as a source document: its canonical JSON, plus text."""
    data = canonical_json(form).encode("utf-8")
    lines = [f"{key}: {value}" for key, value in _flatten(form) if value not in (None, "")]
    return IncomingDocument(
        part="form",
        content_type="application/json",
        data=data,
        filename="form.json",
        text="\n".join(lines),
    )


def _flatten(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key in value:
            items += _flatten(
                value[key], f"{prefix}{key}." if isinstance(value[key], dict) else f"{prefix}{key}"
            )
        return items
    return [(prefix, json.dumps(value) if isinstance(value, list) else value)]
