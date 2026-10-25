"""Intake APIs: REST reports, web/call-centre forms, file uploads, and source retrieval.

Every POST needs an ``Idempotency-Key`` header. Repeating a request with the same key and
the same content returns the original acknowledgment (HTTP 200, ``duplicate: true``) and
creates nothing. Reusing a key for different content is refused (HTTP 422).
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from pharmasafe_api.cases.router import CASE_ROLES
from pharmasafe_api.deps import TenantSession, require_roles
from pharmasafe_api.intake import service
from pharmasafe_api.intake.models import IntakeSubmissionRow, SourceDocumentRow
from pharmasafe_api.intake.service import Channel, IncomingDocument, IngestResult, Submission
from pharmasafe_domain.case import AgeUnit, CountryCode, Qualification, SafetyCase, Sex
from pharmasafe_domain.dates import PartialDate
from pharmasafe_domain.provenance import Tracked
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.auth import Principal, Role
from pharmasafe_platform.blobstore import BlobError, BlobIntegrityError, sha256_hex

router = APIRouter(prefix="/intake", tags=["intake"])

Receiver = Annotated[Principal, Depends(require_roles(Role.INTAKE_PROCESSOR, Role.CASE_PROCESSOR))]
Reader = Annotated[Principal, Depends(require_roles(*CASE_ROLES))]
IdempotencyKey = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9._:-]+$",
        description="Client-generated unique key (e.g. a UUID) for this report",
    ),
]

MAX_TEXT_CHARS = 1_000_000


# ---------------------------------------------------------------- requests


class ReportText(BaseModel):
    """A report as text, from a customer system or partner."""

    text: str = Field(min_length=1, max_length=MAX_TEXT_CHARS)
    source_reference: str | None = Field(default=None, max_length=200)


class FormReporter(BaseModel):
    given_name: str | None = Field(default=None, max_length=200)
    family_name: str | None = Field(default=None, max_length=200)
    email: str | None = Field(default=None, max_length=200)
    telephone: str | None = Field(default=None, max_length=50)
    country: CountryCode | None = None
    qualification: Qualification | None = None


class FormPatient(BaseModel):
    initials: str | None = Field(default=None, max_length=20)
    age: Decimal | None = Field(default=None, ge=0, le=150)
    age_unit: AgeUnit = AgeUnit.YEAR
    sex: Sex | None = None


class IntakeForm(BaseModel):
    """The web and call-centre form. Every field is optional; intake never refuses a report."""

    reporter: FormReporter = FormReporter()
    patient: FormPatient = FormPatient()
    product_name: str | None = Field(default=None, max_length=200)
    reaction: str | None = Field(default=None, max_length=500)
    onset_date: PartialDate | None = None
    occurrence_country: CountryCode | None = None
    description: str | None = Field(default=None, max_length=20000)


# ---------------------------------------------------------------- responses


class DocumentOut(BaseModel):
    id: UUID
    position: int
    part: str
    filename: str | None
    content_type: str
    size: int
    sha256: str
    text_status: str | None
    language: str | None


class ReportOut(BaseModel):
    index: int
    case_id: UUID
    case_number: str
    document_id: UUID | None
    text_start: int | None
    text_end: int | None
    language: str


class Acknowledgment(BaseModel):
    submission_id: UUID
    channel: str
    received_at: datetime
    duplicate: bool
    documents: list[DocumentOut]
    reports: list[ReportOut]


# ---------------------------------------------------------------- endpoints


@router.post("/reports", response_model=Acknowledgment, status_code=status.HTTP_201_CREATED)
def submit_report(
    body: ReportText,
    key: IdempotencyKey,
    principal: Receiver,
    session: TenantSession,
    request: Request,
    response: Response,
) -> Acknowledgment:
    document = IncomingDocument(
        part="text", content_type="text/plain; charset=utf-8", data=body.text.encode("utf-8")
    )
    submission = Submission(
        channel=Channel.API,
        external_id=key,
        request_hash=service.request_hash(body.model_dump(mode="json")),
        documents=[document],
        subject=body.source_reference,
    )
    return _ingest(session, principal, submission, request, response)


@router.post("/forms", response_model=Acknowledgment, status_code=status.HTTP_201_CREATED)
def submit_form(
    form: IntakeForm,
    key: IdempotencyKey,
    principal: Receiver,
    session: TenantSession,
    request: Request,
    response: Response,
) -> Acknowledgment:
    data = form.model_dump(mode="json")
    submission = Submission(
        channel=Channel.FORM,
        external_id=key,
        request_hash=service.request_hash(data),
        documents=[service.form_document(data)],
        case_content=_form_case(form, principal),
    )
    return _ingest(session, principal, submission, request, response)


@router.post("/uploads", response_model=Acknowledgment, status_code=status.HTTP_201_CREATED)
def upload(
    files: Annotated[list[UploadFile], File(description="Report files: PDF, images, text, email")],
    key: IdempotencyKey,
    principal: Receiver,
    session: TenantSession,
    request: Request,
    response: Response,
) -> Acknowledgment:
    limit: int = request.app.state.settings.intake_max_file_bytes
    documents = []
    for file in files:
        data = file.file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(status.HTTP_413_CONTENT_TOO_LARGE, detail="file is too large")
        content_type = file.content_type or "application/octet-stream"
        documents.append(
            IncomingDocument(
                part="upload", content_type=content_type, data=data, filename=file.filename
            )
        )
    submission = Submission(
        channel=Channel.UPLOAD,
        external_id=key,
        request_hash=service.request_hash(
            [[d.filename, d.content_type, sha256_hex(d.data)] for d in documents]
        ),
        documents=documents,
    )
    return _ingest(session, principal, submission, request, response)


@router.get("/submissions/{submission_id}", response_model=Acknowledgment)
def get_submission(
    submission_id: UUID, principal: Reader, session: TenantSession
) -> Acknowledgment:
    try:
        row = service.visible_submission(session, submission_id, principal.scope)
    except service.SubmissionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="submission not found") from exc
    return _acknowledgment(session, row.id, duplicate=False)


@router.get("/documents/{document_id}/content")
def document_content(
    document_id: UUID, principal: Reader, session: TenantSession, request: Request
) -> Response:
    """The source document exactly as received, verified against its recorded SHA-256."""
    try:
        row, data = service.document_content(
            session, document_id, principal.scope, request.app.state.blobs
        )
    except service.SubmissionNotFound as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="document not found") from exc
    except BlobIntegrityError as exc:
        raise HTTPException(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="stored document failed its integrity check",
        ) from exc
    except BlobError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, detail="source storage unavailable"
        ) from exc
    return Response(
        content=data,
        media_type=row.content_type,
        headers={
            # Never render untrusted content in the browser (e.g. HTML from an email).
            "Content-Disposition": f'attachment; filename="{_safe_filename(row)}"',
            "X-Content-Type-Options": "nosniff",
            "X-Content-SHA256": row.sha256,
        },
    )


# ---------------------------------------------------------------- helpers


def _deps(request: Request) -> service.IntakeDeps:
    state = request.app.state
    return service.IntakeDeps(blobs=state.blobs, ocr=state.ocr, registry=state.clock_rules)


def _ingest(
    session: Session,
    principal: Principal,
    submission: Submission,
    request: Request,
    response: Response,
) -> Acknowledgment:
    result = _run_ingest(session, principal, submission, request)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return _acknowledgment(session, result.submission.id, duplicate=not result.created)


def _run_ingest(
    session: Session, principal: Principal, submission: Submission, request: Request
) -> IngestResult:
    try:
        return service.ingest(
            session,
            principal.require_tenant(),
            Actor.from_principal(principal),
            submission,
            _deps(request),
        )
    except service.IdempotencyConflict as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, detail=str(exc)) from exc
    except BlobError as exc:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="source storage unavailable; retry with the same Idempotency-Key",
        ) from exc


def _acknowledgment(session: Session, submission_id: UUID, *, duplicate: bool) -> Acknowledgment:
    row = session.get_one(IntakeSubmissionRow, submission_id)
    documents = service.documents_of(session, row.id)
    texts = service.texts_of(session, [d.id for d in documents])
    return Acknowledgment(
        submission_id=row.id,
        channel=row.channel,
        received_at=row.received_at,
        duplicate=duplicate,
        documents=[
            DocumentOut(
                id=d.id,
                position=d.position,
                part=d.part,
                filename=d.filename,
                content_type=d.content_type,
                size=d.size,
                sha256=d.sha256,
                text_status=texts[d.id].status if d.id in texts else None,
                language=texts[d.id].language if d.id in texts else None,
            )
            for d in documents
        ],
        reports=[
            ReportOut(
                index=r.report_index,
                case_id=r.case_id,
                case_number=number,
                document_id=r.document_id,
                text_start=r.text_start,
                text_end=r.text_end,
                language=r.language,
            )
            for r, number in service.reports_of(session, row.id)
        ],
    )


def _form_case(form: IntakeForm, principal: Principal) -> SafetyCase:
    """Map form fields onto the case, entered (and so confirmed) by the intake user."""
    now = datetime.now(UTC)

    def h(value: Any) -> dict[str, Any] | None:
        if value in (None, ""):
            return None
        return Tracked[Any].human(value, by=principal.subject, at=now).model_dump(mode="json")

    r, p = form.reporter, form.patient
    content: dict[str, Any] = {
        "admin": {"occurrence_country": h(form.occurrence_country)},
        "patient": {
            "initials": h(p.initials),
            "sex": h(p.sex),
            "age": h(
                {"value": str(p.age), "unit": p.age_unit.value} if p.age is not None else None
            ),
        },
        "narrative": {"reporter_comments": h(form.description)},
    }
    reporter = {
        "given_name": h(r.given_name),
        "family_name": h(r.family_name),
        "email": h(r.email),
        "telephone": h(r.telephone),
        "country": h(r.country),
        "qualification": h(r.qualification),
    }
    if any(reporter.values()):
        content["reporters"] = [{"id": "p1", **reporter}]
    if form.product_name:
        # The form asks for the medicine the reporter suspects.
        content["drugs"] = [
            {"id": "d1", "role": h("suspect"), "product_name": h(form.product_name)}
        ]
    if form.reaction:
        content["reactions"] = [
            {"id": "r1", "reported_term": h(form.reaction), "onset_date": h(form.onset_date)}
        ]
    return SafetyCase.from_json(content)


def _safe_filename(row: SourceDocumentRow) -> str:
    name = row.filename or f"{row.part}-{row.position}"
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in name)[:100]
