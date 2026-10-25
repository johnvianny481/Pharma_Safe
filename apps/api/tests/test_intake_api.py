"""Intake through the API: idempotency, sources kept byte for byte, splitting, isolation."""

import secrets
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.blobstore import sha256_hex

TokenFactory = Callable[..., str]


def _headers(
    issue_token: TokenFactory, tenant: UUID, key: str | None = None, **claims: Any
) -> dict[str, str]:
    token = issue_token(**({"tenant_id": str(tenant), "roles": ["intake_processor"]} | claims))
    headers = {"Authorization": f"Bearer {token}"}
    if key is not None:
        headers["Idempotency-Key"] = key
    return headers


def _key() -> str:
    return f"key-{secrets.token_hex(8)}"


def test_report_with_the_same_key_creates_one_case(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    key = _key()
    body = {"text": "The patient took Acmezol and developed a rash.", "source_reference": "CRM-1"}
    first = intake_client.post(
        "/intake/reports", json=body, headers=_headers(issue_token, tenant_a, key)
    )
    assert first.status_code == 201, first.text
    ack = first.json()
    assert ack["duplicate"] is False
    assert len(ack["reports"]) == 1
    assert ack["reports"][0]["language"] == "en"

    again = intake_client.post(
        "/intake/reports", json=body, headers=_headers(issue_token, tenant_a, key)
    )
    assert again.status_code == 200
    assert again.json()["duplicate"] is True
    assert again.json()["submission_id"] == ack["submission_id"]
    assert again.json()["reports"] == ack["reports"]

    cases = intake_client.get("/cases", headers=_headers(issue_token, tenant_a)).json()
    assert [c["id"] for c in cases] == [ack["reports"][0]["case_id"]]


def test_reusing_a_key_for_different_content_is_refused(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    key = _key()
    headers = _headers(issue_token, tenant_a, key)
    assert (
        intake_client.post(
            "/intake/reports", json={"text": "first report"}, headers=headers
        ).status_code
        == 201
    )
    second = intake_client.post("/intake/reports", json={"text": "another report"}, headers=headers)
    assert second.status_code == 422
    assert "Idempotency-Key" in second.json()["detail"]


def test_intake_endpoints_require_an_idempotency_key(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    headers = _headers(issue_token, tenant_a)
    assert (
        intake_client.post("/intake/reports", json={"text": "x"}, headers=headers).status_code
        == 422
    )
    assert intake_client.post("/intake/forms", json={}, headers=headers).status_code == 422
    files = {"files": ("a.txt", b"x", "text/plain")}
    assert intake_client.post("/intake/uploads", files=files, headers=headers).status_code == 422


def test_uploaded_files_are_retrievable_byte_for_byte(
    intake_client: TestClient,
    issue_token: TokenFactory,
    tenant_a: UUID,
    make_pdf: Callable[[list[str]], bytes],
) -> None:
    pdf = make_pdf(["Patient JD took Acmezol 5 mg and developed a rash on day two."])
    image = b"\x89PNG\r\n\x1a\nPatient seen in clinic"  # FakeOcr reads bytes as text
    binary = bytes(range(256)) * 50
    files = [
        ("files", ("report.pdf", pdf, "application/pdf")),
        ("files", ("photo.png", image, "image/png")),
        ("files", ("export.bin", binary, "application/octet-stream")),
    ]
    response = intake_client.post(
        "/intake/uploads", files=files, headers=_headers(issue_token, tenant_a, _key())
    )
    assert response.status_code == 201, response.text
    documents = response.json()["documents"]
    assert [(d["filename"], d["text_status"]) for d in documents] == [
        ("report.pdf", "extracted"),
        ("photo.png", "extracted"),
        ("export.bin", "unsupported"),
    ]
    reader = _headers(issue_token, tenant_a, roles=["case_processor"])
    for document, original in zip(documents, (pdf, image, binary), strict=True):
        assert document["sha256"] == sha256_hex(original)
        content = intake_client.get(f"/intake/documents/{document['id']}/content", headers=reader)
        assert content.status_code == 200
        assert content.content == original
        assert content.headers["x-content-sha256"] == sha256_hex(original)
        assert content.headers["content-disposition"].startswith("attachment;")
        assert content.headers["x-content-type-options"] == "nosniff"


def test_an_uploaded_image_is_read_by_tesseract_into_a_case(
    ocr_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    image = (Path(__file__).parent / "fixtures" / "ocr_report.png").read_bytes()
    response = ocr_client.post(
        "/intake/uploads",
        files=[("files", ("scan.png", image, "image/png"))],
        headers=_headers(issue_token, tenant_a, _key()),
    )
    assert response.status_code == 201, response.text
    ack = response.json()
    [document] = ack["documents"]
    assert (document["text_status"], document["language"]) == ("extracted", "en")
    [report] = ack["reports"]
    assert report["text_end"] - report["text_start"] > 50  # the recognised text, not empty


def test_a_document_with_several_reports_becomes_several_cases(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    text_body = (
        "Line listing\n\nCase 1\nPatient JD developed a rash after Acmezol.\n\n"
        "Case 2\nPatient KL had a headache after Acmezol.\n\n"
        "Case 3\nDer Patient wurde nach der Einnahme von Acmezol mit Leberschaden ins Krankenhaus eingeliefert und die Behandlung wurde abgebrochen.\n"
    )
    ack = intake_client.post(
        "/intake/reports", json={"text": text_body}, headers=_headers(issue_token, tenant_a, _key())
    ).json()
    reports = ack["reports"]
    assert [r["index"] for r in reports] == [0, 1, 2]
    assert len({r["case_id"] for r in reports}) == 3
    spans = [text_body[r["text_start"] : r["text_end"]] for r in reports]
    assert spans[0].startswith("Case 1")
    assert spans[2].startswith("Case 3")
    assert reports[2]["language"] == "de"


def test_form_fields_become_case_values_entered_by_the_intake_user(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    form = {
        "reporter": {"given_name": "Ana", "qualification": "consumer", "country": "DE"},
        "patient": {"initials": "JD", "age": "42", "sex": "female"},
        "product_name": "Acmezol",
        "reaction": "rash",
        "onset_date": "2026-09",
        "occurrence_country": "DE",
        "description": "Rash on both arms two days after the first tablet.",
    }
    headers = _headers(issue_token, tenant_a, _key(), sub="intake-1")
    ack = intake_client.post("/intake/forms", json=form, headers=headers).json()
    case_id = ack["reports"][0]["case_id"]
    assert ack["documents"][0]["part"] == "form"

    reader = _headers(issue_token, tenant_a, roles=["case_processor"])
    content = intake_client.get(f"/cases/{case_id}/versions/1", headers=reader).json()["content"]
    assert content["patient"]["initials"]["value"] == "JD"
    assert content["patient"]["initials"]["confirmed_by"] == "intake-1"
    assert content["drugs"][0]["role"]["value"] == "suspect"
    assert content["reactions"][0]["onset_date"]["value"] == "2026-09"
    receipt = content["admin"]["latest_receipt_date"]
    assert (receipt["source"], receipt["confirmed_by"]) == ("import", None)  # a person must confirm

    assessment = intake_client.get(f"/cases/{case_id}/versions/1/assessment", headers=reader).json()
    assert assessment["valid"] is True
    assert assessment["day_zero"] == receipt["value"]
    blockers = {(b["code"], b["path"]) for b in assessment["approval_blockers"]}
    assert ("unconfirmed_value", "/admin/latest_receipt_date") in blockers


def test_intake_is_audited_with_source_hashes_and_announced(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID, owner_engine: Engine
) -> None:
    ack = intake_client.post(
        "/intake/reports",
        json={"text": "Rash after Acmezol."},
        headers=_headers(issue_token, tenant_a, _key()),
    ).json()
    admin = _headers(issue_token, tenant_a, roles=["tenant_admin"])
    entries = intake_client.get(
        "/audit/entries",
        params={"entity_type": "intake_submission", "entity_id": ack["submission_id"]},
        headers=admin,
    ).json()
    (received,) = entries
    assert received["action"] == "intake.received"
    assert received["new_value"]["documents"][0]["sha256"] == ack["documents"][0]["sha256"]
    with owner_engine.connect() as conn:
        events = (
            conn.execute(
                text("SELECT event_type FROM outbox WHERE aggregate_id = :s"),
                {"s": ack["submission_id"]},
            )
            .scalars()
            .all()
        )
    assert events == ["intake.received"]


def test_sources_are_isolated_by_tenant_and_scope(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID, tenant_b: UUID
) -> None:
    ack = intake_client.post(
        "/intake/reports",
        json={"text": "Rash after Acmezol."},
        headers=_headers(issue_token, tenant_a, _key()),
    ).json()
    document = f"/intake/documents/{ack['documents'][0]['id']}/content"
    submission = f"/intake/submissions/{ack['submission_id']}"

    other_tenant = _headers(issue_token, tenant_b, roles=["case_processor"])
    assert intake_client.get(document, headers=other_tenant).status_code == 404
    assert intake_client.get(submission, headers=other_tenant).status_code == 404

    # The case has no country or product yet, so only unrestricted users see its sources.
    restricted = _headers(
        issue_token, tenant_a, roles=["case_processor"], ps_products=["ACM-001"], ps_regions=["eu"]
    )
    assert intake_client.get(document, headers=restricted).status_code == 404
    unrestricted = _headers(issue_token, tenant_a, roles=["case_processor"])
    assert (
        intake_client.get(submission, headers=unrestricted).json()["submission_id"]
        == ack["submission_id"]
    )


def test_intake_needs_an_intake_role(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID
) -> None:
    for role in ("quality_reviewer", "medical_reviewer", "tenant_admin"):
        headers = _headers(issue_token, tenant_a, _key(), roles=[role])
        assert (
            intake_client.post("/intake/reports", json={"text": "x"}, headers=headers).status_code
            == 403
        )


def test_source_records_are_append_only(
    intake_client: TestClient, issue_token: TokenFactory, tenant_a: UUID, owner_engine: Engine
) -> None:
    ack = intake_client.post(
        "/intake/reports",
        json={"text": "Rash after Acmezol."},
        headers=_headers(issue_token, tenant_a, _key()),
    ).json()
    for statement in (
        "UPDATE source_documents SET sha256 = repeat('0', 64) WHERE submission_id = :s",
        "DELETE FROM intake_reports WHERE submission_id = :s",
        "UPDATE document_texts SET text = 'edited'",
    ):
        with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
            conn.execute(text(statement), {"s": ack["submission_id"]})
