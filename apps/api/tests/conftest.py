from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from httpx import Response
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pharmasafe_api.main import create_app
from pharmasafe_platform.auth import TokenVerifier
from pharmasafe_platform.db import Database
from pharmasafe_platform.telemetry import ContextSpanProcessor


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def client(
    db: Database, verifier: TokenVerifier, spans: InMemorySpanExporter
) -> Iterator[TestClient]:
    provider = TracerProvider()
    provider.add_span_processor(ContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(spans))
    app = create_app(verifier=verifier, database=db, tracer_provider=provider)
    with TestClient(app) as test_client:
        yield test_client


# ---------------------------------------------------------------- case API helpers


@dataclass
class CasesApi:
    """Thin client for the case endpoints, acting as users of one tenant."""

    client: TestClient
    issue_token: Callable[..., str]
    tenant: UUID

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

    def assessment(self, case_id: str, version: int = 1) -> dict[str, Any]:
        response = self.client.get(
            f"/cases/{case_id}/versions/{version}/assessment", headers=self.headers()
        )
        assert response.status_code == 200, response.text
        body: dict[str, Any] = response.json()
        return body


@pytest.fixture
def cases_api(client: TestClient, issue_token: Callable[..., str], tenant_a: UUID) -> CasesApi:
    return CasesApi(client, issue_token, tenant_a)


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
