from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pharmasafe_api.cases import service as cases_service
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.intake.extraction import OcrEngine, TesseractOcr
from pharmasafe_api.main import create_app
from pharmasafe_api.review import service as review
from pharmasafe_domain.rules.review import Step
from pharmasafe_platform.auth import TokenVerifier
from pharmasafe_platform.blobstore import AzureBlobStore, BlobStore, InMemoryBlobStore
from pharmasafe_platform.db import Database
from pharmasafe_platform.telemetry import ContextSpanProcessor


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


class FakeOcr:
    """Stands in for Tesseract: "reads" an image whose bytes are UTF-8 text."""

    name = "fake"

    def supports(self, content_type: str) -> bool:
        return content_type.startswith("image/")

    def recognize(self, data: bytes, content_type: str) -> str:
        return data.decode("utf-8", errors="replace")


def _make_client(
    db: Database,
    verifier: TokenVerifier,
    spans: InMemorySpanExporter,
    blobs: BlobStore,
    ocr: OcrEngine | None = None,
) -> TestClient:
    provider = TracerProvider()
    provider.add_span_processor(ContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(spans))
    app = create_app(
        verifier=verifier, database=db, blobs=blobs, ocr=ocr or FakeOcr(), tracer_provider=provider
    )
    return TestClient(app)


@pytest.fixture
def client(
    db: Database, verifier: TokenVerifier, spans: InMemorySpanExporter
) -> Iterator[TestClient]:
    """The API with in-memory source storage (no Azurite needed)."""
    with _make_client(db, verifier, spans, InMemoryBlobStore()) as test_client:
        yield test_client


@pytest.fixture
def intake_client(
    db: Database, verifier: TokenVerifier, spans: InMemorySpanExporter, blob_store: AzureBlobStore
) -> Iterator[TestClient]:
    """The API with real immutable source storage (Azurite)."""
    with _make_client(db, verifier, spans, blob_store) as test_client:
        yield test_client


@pytest.fixture
def ocr_client(
    db: Database, verifier: TokenVerifier, spans: InMemorySpanExporter, tesseract_cmd: str
) -> Iterator[TestClient]:
    """The API with real Tesseract OCR, found as the app finds it."""
    ocr = TesseractOcr.find(tesseract_cmd)
    assert ocr is not None
    with _make_client(db, verifier, spans, InMemoryBlobStore(), ocr) as test_client:
        yield test_client


# ---------------------------------------------------------------- case API helpers


@dataclass
class CasesApi:
    """Thin client for the case endpoints, acting as users of one tenant."""

    client: TestClient
    issue_token: Callable[..., str]
    tenant: UUID
    db: Database

    def headers(
        self, roles: Sequence[str] = ("case_processor",), sub: str = "proc-1", **claims: Any
    ) -> dict[str, str]:
        defaults: dict[str, Any] = {"sub": sub, "tenant_id": str(self.tenant), "roles": list(roles)}
        token = self.issue_token(**(defaults | claims))
        return {"Authorization": f"Bearer {token}"}

    def create(self, ops: list[dict[str, Any]], **who: Any) -> dict[str, Any]:
        response = self.client.post("/cases", json={"changes": ops}, headers=self.headers(**who))
        assert response.status_code == 201, response.text
        body: dict[str, Any] = response.json()
        return body

    def version(self, case_id: str, version: int = 1) -> dict[str, Any]:
        response = self.client.get(f"/cases/{case_id}/versions/{version}", headers=self.headers())
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body

    def edit(self, case_id: str, version: int, ops: list[dict[str, Any]], *, revision: int | None = None,
             reason: str | None = None, **who: Any) -> Response:  # fmt: skip
        if revision is None:
            revision = self.version(case_id, version)["revision"]
        body = {"expected_revision": revision, "changes": ops, "reason": reason}
        return self.client.patch(
            f"/cases/{case_id}/versions/{version}", json=body, headers=self.headers(**who)
        )

    def approve(self, case_id: str, version: int = 1, sub: str = "qa-1") -> Response:
        roles = ["quality_reviewer"]
        reauth = self.issue_token(sub=sub, tenant_id=str(self.tenant), roles=roles)
        return self.client.post(
            f"/cases/{case_id}/versions/{version}/approve",
            json={"reauth_token": reauth, "reason": "data verified"},
            headers=self.headers(roles=roles, sub=sub),
        )

    def open_task(self, case_id: str, version: int, kind: str) -> str:
        """Open a review task, as the case workflow's activity would."""
        with self.db.tenant_session(self.tenant) as session:
            case = session.get_one(CaseRow, UUID(case_id))
            task = review.open_task(
                session, case, version, review.Kind(kind), uuid4(), datetime.now(UTC)
            )
            return str(task.id)

    def complete_task(self, task_id: str, sub: str, role: str) -> Response:
        reauth = self.issue_token(sub=sub, tenant_id=str(self.tenant), roles=[role])
        return self.client.post(
            f"/tasks/{task_id}/complete",
            json={"reauth_token": reauth, "reason": "checked"},
            headers=self.headers(roles=[role], sub=sub),
        )

    def complete_review(self, case_id: str, version: int = 1) -> None:
        """Sign every review step the version needs before approval, as different people."""
        with self.db.tenant_session(self.tenant) as session:
            case = session.get_one(CaseRow, UUID(case_id))
            plan = cases_service.review_plan_of(
                session, case, cases_service.get_version(session, case.id, version)
            )
        for step in plan.steps[:-1]:
            sub, role = REVIEWERS[step]
            response = self.complete_task(
                self.open_task(case_id, version, review.KIND_OF[step]), sub, role
            )
            assert response.status_code == 200, response.text

    def assessment(self, case_id: str, version: int = 1) -> dict[str, Any]:
        response = self.client.get(
            f"/cases/{case_id}/versions/{version}/assessment", headers=self.headers()
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
def cases_api(
    client: TestClient, issue_token: Callable[..., str], tenant_a: UUID, db: Database
) -> CasesApi:
    return CasesApi(client, issue_token, tenant_a, db)


# Who signs each review step in tests. The approver (qa-1) is someone else again.
REVIEWERS = {
    Step.DATA_ENTRY: ("proc-1", "case_processor"),
    Step.QUALITY: ("qa-2", "quality_reviewer"),
    Step.MEDICAL: ("doc-1", "medical_reviewer"),
}


def set_op(path: str, value: Any) -> dict[str, Any]:
    return {"op": "set", "path": path, "value": value}


def valid_case_ops(
    receipt: date, *, serious: bool = False, country: str = "US", product: str = "ACM-001"
) -> list[dict[str, Any]]:
    """Operations that build a complete, approvable case through the API."""
    criteria = ["death", "life_threatening", "hospitalisation", "disability",
                "congenital_anomaly", "other_medically_important"]  # fmt: skip
    return [
        set_op("/admin/report_type", "spontaneous"),
        set_op("/admin/initial_receipt_date", receipt.isoformat()),
        set_op("/admin/latest_receipt_date", receipt.isoformat()),
        set_op("/admin/occurrence_country", country),
        set_op("/patient/initials", "JD"),
        set_op("/patient/sex", "female"),
        {"op": "add", "path": "/reporters", "id": "p1"},
        set_op("/reporters/p1/qualification", "physician"),
        {"op": "add", "path": "/drugs", "id": "d1"},
        set_op("/drugs/d1/role", "suspect"),
        set_op("/drugs/d1/product_name", "Acmezol"),
        set_op("/drugs/d1/product_code", product),
        {"op": "add", "path": "/reactions", "id": "r1"},
        set_op("/reactions/r1/reported_term", "rash"),
        set_op("/reactions/r1/expectedness", "unexpected"),
        *(
            set_op(f"/reactions/r1/seriousness/{c}", serious and c == "hospitalisation")
            for c in criteria
        ),
    ]


@pytest.fixture(scope="session")
def case_ops() -> Callable[..., list[dict[str, Any]]]:
    return valid_case_ops


@pytest.fixture(scope="session")
def op() -> Callable[[str, Any], dict[str, Any]]:
    return set_op


# ---------------------------------------------------------------- intake helpers


def build_pdf(lines: list[str]) -> bytes:
    """A minimal one-page PDF with real (extractable) text."""
    content = (
        "BT /F1 12 Tf 72 720 Td 14 TL " + " ".join(f"({line}) Tj T*" for line in lines) + " ET"
    )
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        f"<< /Length {len(content)} >>\nstream\n{content}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = b"%PDF-1.4\n"
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n{body}\nendobj\n".encode("latin-1")
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += "".join(f"{offset:010d} 00000 n \n" for offset in offsets).encode()
    out += (
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return out


@pytest.fixture(scope="session")
def make_pdf() -> Callable[[list[str]], bytes]:
    return build_pdf
