"""HTTP-level checks: authentication, roles, and tenant isolation through the API."""

from collections.abc import Callable
from uuid import UUID

from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pharmasafe_platform.audit import Actor, AuditEvent, append_audit
from pharmasafe_platform.db import Database

TokenFactory = Callable[..., str]


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _record(db: Database, tenant: UUID, entity_id: str) -> None:
    with db.tenant_session(tenant) as session:
        append_audit(
            session,
            tenant,
            AuditEvent(Actor("u1", "User One"), "test.event", "case", entity_id),
        )


def test_me_requires_a_token(client: TestClient) -> None:
    response = client.get("/me")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_me_rejects_a_bad_token(client: TestClient) -> None:
    assert client.get("/me", headers=_bearer("not.a.jwt")).status_code == 401


def test_me_returns_identity_from_the_token(client: TestClient, issue_token: TokenFactory) -> None:
    tenant = "11111111-1111-4111-8111-111111111111"
    token = issue_token(
        tenant_id=tenant, roles=["case_processor"], ps_products=["ACM-001"], ps_regions=["*"]
    )
    body = client.get("/me", headers=_bearer(token)).json()
    assert body["tenant_id"] == tenant
    assert body["roles"] == ["case_processor"]
    assert body["products"] == ["ACM-001"]
    assert body["regions"] is None


def test_audit_entries_are_isolated_by_token_tenant(
    client: TestClient, db: Database, issue_token: TokenFactory, tenant_a: UUID, tenant_b: UUID
) -> None:
    _record(db, tenant_a, "case-a")
    _record(db, tenant_b, "case-b")
    token_a = issue_token(tenant_id=str(tenant_a), roles=["tenant_admin"])

    # Attempts to pick the other tenant through the request are ignored: tenant comes from the token.
    response = client.get(
        "/audit/entries",
        params={"tenant_id": str(tenant_b)},
        headers={**_bearer(token_a), "X-Tenant-Id": str(tenant_b)},
    )
    assert response.status_code == 200
    assert [e["entity_id"] for e in response.json()] == ["case-a"]


def test_audit_entries_need_an_audit_role(
    client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    token = issue_token(tenant_id=str(tenant_a), roles=["case_processor"])
    assert client.get("/audit/entries", headers=_bearer(token)).status_code == 403


def test_platform_admin_cannot_read_tenant_data(
    client: TestClient, issue_token: TokenFactory
) -> None:
    token = issue_token(tenant_id=None, roles=["platform_admin", "tenant_admin"])
    response = client.get("/audit/entries", headers=_bearer(token))
    assert response.status_code == 403


def test_tenant_admin_can_verify_chain(
    client: TestClient, db: Database, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    _record(db, tenant_a, "case-a")
    _record(db, tenant_a, "case-a")
    token = issue_token(tenant_id=str(tenant_a), roles=["tenant_admin"])
    body = client.post("/audit/verify", headers=_bearer(token)).json()
    assert body == {"ok": True, "entries_checked": 2, "first_invalid_seq": None, "problem": None}


def test_quality_reviewer_cannot_run_verification(
    client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    token = issue_token(tenant_id=str(tenant_a), roles=["quality_reviewer"])
    assert client.post("/audit/verify", headers=_bearer(token)).status_code == 403


def test_request_spans_carry_tenant_id(
    client: TestClient,
    db: Database,
    issue_token: TokenFactory,
    tenant_a: UUID,
    spans: InMemorySpanExporter,
) -> None:
    token = issue_token(tenant_id=str(tenant_a), roles=["tenant_admin"])
    client.get("/audit/entries", headers=_bearer(token))
    finished = spans.get_finished_spans()
    server = [s for s in finished if s.name.startswith("GET /audit/entries")]
    assert server, [s.name for s in finished]
    assert all(s.attributes and s.attributes.get("tenant_id") == str(tenant_a) for s in server)
