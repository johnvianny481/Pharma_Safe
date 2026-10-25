"""Case APIs end to end: provenance, versioning, approval, follow-ups, rebuilds, isolation."""

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_platform.audit import verify_chain
from pharmasafe_platform.db import Database
from pharmasafe_platform.signatures import content_hash

Ops = Callable[..., list[dict[str, Any]]]
Op = Callable[[str, Any], dict[str, Any]]
RECEIPT = datetime.now(UTC).date() - timedelta(days=10)


def _approved_case(cases_api: Any, case_ops: Ops, **kw: Any) -> dict[str, Any]:
    case = cases_api.create(case_ops(RECEIPT, **kw))
    response = cases_api.approve(case["id"])
    assert response.status_code == 200, response.text
    return case


def test_values_entered_through_the_api_are_human_and_confirmed(cases_api: Any, op: Op) -> None:
    case = cases_api.create([op("/patient/initials", "JD")], sub="proc-7")
    assert case["case_number"].startswith("PS-")
    assert case["versions"][0]["status"] == "draft"
    initials = cases_api.version(case["id"])["content"]["patient"]["initials"]
    assert initials["value"] == "JD"
    assert initials["source"] == "human"
    assert initials["confirmed_by"] == "proc-7"


def test_clients_cannot_forge_provenance(cases_api: Any, op: Op) -> None:
    """Whatever a client sends is the value itself; provenance fields are never accepted."""
    forged = {"value": "JD", "source": "ai", "confidence": 1.0, "confirmed_by": "someone-else"}
    for path, value in (
        ("/patient/initials", forged),
        ("/patient/age", {"value": "4", "unit": "a", "source": "ai"}),
    ):
        response = cases_api.client.post(
            "/cases", json={"changes": [op(path, value)]}, headers=cases_api.headers()
        )
        assert response.status_code == 422, path


def test_assessment_reports_validity_seriousness_and_clocks(cases_api: Any, case_ops: Ops) -> None:
    case = cases_api.create(case_ops(RECEIPT, serious=True))
    result = cases_api.assessment(case["id"])
    assert result["valid"] is True
    assert result["serious"] is True
    assert result["approval_blockers"] == []
    assert result["day_zero"] == RECEIPT.isoformat()
    clocks = {(c["regulator"], c["rule_id"]): c for c in result["clocks"]}
    alert = clocks[("FDA", "postmarketing-15-day-alert")]
    assert alert["due_date"] == (RECEIPT + timedelta(days=15)).isoformat()
    assert ("EMA", "icsr-serious") in clocks  # demo tenants use FDA and EMA


def test_incomplete_case_is_invalid_and_cannot_be_approved(cases_api: Any, op: Op) -> None:
    case = cases_api.create([op("/patient/initials", "JD")])
    result = cases_api.assessment(case["id"])
    assert result["valid"] is False
    assert set(result["missing_criteria"]) == {
        "identifiable_reporter",
        "suspect_drug",
        "adverse_reaction",
    }
    assert result["day_zero"] is None
    response = cases_api.approve(case["id"])
    assert response.status_code == 422
    codes = {i["code"] for i in response.json()["detail"]["issues"]}
    assert {"invalid_case", "missing_receipt_date"} <= codes


def test_stale_revision_is_rejected(cases_api: Any, op: Op) -> None:
    case = cases_api.create([])
    first = cases_api.edit(case["id"], 1, [op("/patient/initials", "AB")], revision=0)
    assert first.status_code == 200
    stale = cases_api.edit(case["id"], 1, [op("/patient/initials", "CD")], revision=0)
    assert stale.status_code == 409


def test_invalid_changes_are_rejected_atomically(cases_api: Any, op: Op) -> None:
    case = cases_api.create([])
    response = cases_api.edit(
        case["id"], 1, [op("/patient/initials", "AB"), op("/patient/age", "old")]
    )
    assert response.status_code == 422
    assert cases_api.version(case["id"])["content"]["patient"]["initials"] is None


def test_confirm_keeps_the_value_and_records_the_confirmer(cases_api: Any, op: Op) -> None:
    case = cases_api.create([op("/patient/initials", "JD")], sub="proc-1")
    response = cases_api.edit(
        case["id"], 1, [{"op": "confirm", "path": "/patient/initials"}], sub="proc-2"
    )
    assert response.status_code == 200
    initials = response.json()["content"]["patient"]["initials"]
    assert (initials["value"], initials["confirmed_by"]) == ("JD", "proc-2")
    empty = cases_api.edit(case["id"], 1, [{"op": "confirm", "path": "/patient/sex"}])
    assert empty.status_code == 422


def test_approval_needs_the_right_role_and_a_fresh_reauthentication(
    cases_api: Any, case_ops: Ops, issue_token: Callable[..., str]
) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    url = f"/cases/{case['id']}/versions/1/approve"
    processor = cases_api.headers(roles=["case_processor"])
    assert (
        cases_api.client.post(url, json={"reauth_token": "x"}, headers=processor).status_code == 403
    )

    qa = cases_api.headers(roles=["quality_reviewer"], sub="qa-1")
    stale = issue_token(sub="qa-1", tenant_id=str(cases_api.tenant), roles=["quality_reviewer"],
                        auth_time=int((datetime.now(UTC) - timedelta(hours=1)).timestamp()))  # fmt: skip
    assert cases_api.client.post(url, json={"reauth_token": stale}, headers=qa).status_code == 401
    assert (
        cases_api.client.post(url, json={"reauth_token": "garbage"}, headers=qa).status_code == 401
    )

    fresh = issue_token(sub="qa-1", tenant_id=str(cases_api.tenant), roles=["quality_reviewer"])
    assert cases_api.client.post(url, json={"reauth_token": fresh}, headers=qa).status_code == 200
    again = cases_api.client.post(url, json={"reauth_token": fresh}, headers=qa)
    assert again.status_code == 409  # already approved


def test_approved_version_is_immutable(
    cases_api: Any, case_ops: Ops, op: Op, db: Database, owner_engine: Engine
) -> None:
    case = _approved_case(cases_api, case_ops)
    version = cases_api.version(case["id"])
    assert version["status"] == "approved"
    assert version["signature_id"] is not None

    response = cases_api.edit(case["id"], 1, [op("/patient/initials", "XX")])
    assert response.status_code == 409

    # Not even direct SQL can change it, as the app role or as the schema owner.
    statement = text(
        "UPDATE case_versions SET revision = revision + 1, content = '{}' WHERE case_id = :c"
    )
    with pytest.raises(DBAPIError, match="immutable"), db.tenant_session(cases_api.tenant) as s:
        s.execute(statement, {"c": case["id"]})
    with pytest.raises(DBAPIError, match="immutable"), owner_engine.begin() as conn:
        conn.execute(statement, {"c": case["id"]})
    with pytest.raises(DBAPIError, match="never deleted"), owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM case_versions WHERE case_id = :c"), {"c": case["id"]})


def test_follow_up_creates_a_new_version_and_leaves_the_approved_one_intact(
    cases_api: Any, case_ops: Ops, op: Op
) -> None:
    case = _approved_case(cases_api, case_ops, country="US")
    v1_before = cases_api.version(case["id"], 1)
    follow_up_date = RECEIPT + timedelta(days=5)

    response = cases_api.client.post(
        f"/cases/{case['id']}/follow-ups",
        json={"receipt_date": follow_up_date.isoformat(), "reason": "reporter sent hospital notes"},
        headers=cases_api.headers(),
    )
    assert response.status_code == 201, response.text
    v2 = response.json()
    assert (v2["version"], v2["status"]) == (2, "draft")
    assert v2["content"]["admin"]["latest_receipt_date"]["value"] == follow_up_date.isoformat()

    # The new information makes the case serious: that starts a new clock for version 2.
    edit = cases_api.edit(case["id"], 2, [op("/reactions/r1/seriousness/hospitalisation", True)])
    assert edit.status_code == 200
    clocks = cases_api.assessment(case["id"], 2)["clocks"]
    fda = [c for c in clocks if c["rule_id"] == "postmarketing-15-day-alert"]
    assert [(c["case_version"], c["clock_start"]) for c in fda] == [(2, follow_up_date.isoformat())]
    assert cases_api.assessment(case["id"], 2)["day_zero"] == RECEIPT.isoformat()  # unchanged

    assert cases_api.version(case["id"], 1) == v1_before
    second = cases_api.client.post(
        f"/cases/{case['id']}/follow-ups",
        json={"receipt_date": follow_up_date.isoformat()},
        headers=cases_api.headers(),
    )
    assert second.status_code == 409  # version 2 is still a draft


def test_past_version_is_rebuilt_exactly_from_the_audit_trail(
    cases_api: Any, case_ops: Ops, op: Op
) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    midway = datetime.now(UTC)
    snapshot = cases_api.version(case["id"])["content"]
    cases_api.edit(
        case["id"], 1, [op("/patient/initials", "ZZ"), {"op": "remove", "path": "/reporters/p1"}]
    )
    cases_api.edit(case["id"], 1, [{"op": "add", "path": "/reporters", "id": "p2"},
                                   op("/reporters/p2/qualification", "pharmacist")])  # fmt: skip
    assert cases_api.approve(case["id"]).status_code == 200
    stored = cases_api.version(case["id"])

    auditor = cases_api.headers(roles=["quality_reviewer"])
    now = cases_api.client.get(f"/cases/{case['id']}/versions/1/rebuild", headers=auditor).json()
    assert now["matches_stored"] is True
    assert now["content"] == stored["content"]
    assert content_hash(now["content"]) == stored["content_hash"]

    past = cases_api.client.get(
        f"/cases/{case['id']}/versions/1/rebuild",
        params={"as_of": midway.isoformat()},
        headers=auditor,
    ).json()
    assert past["content"] == snapshot
    assert past["matches_stored"] is None

    before = cases_api.client.get(
        f"/cases/{case['id']}/versions/1/rebuild",
        params={"as_of": (midway - timedelta(days=1)).isoformat()},
        headers=auditor,
    )
    assert before.status_code == 404

    processor = cases_api.headers(roles=["case_processor"])
    assert (
        cases_api.client.get(
            f"/cases/{case['id']}/versions/1/rebuild", headers=processor
        ).status_code
        == 403
    )


def test_every_case_operation_is_audited_in_a_valid_chain(
    cases_api: Any, case_ops: Ops, db: Database
) -> None:
    case = _approved_case(cases_api, case_ops, serious=True)
    entries = cases_api.client.get(
        "/audit/entries",
        params={"entity_type": "case", "entity_id": case["id"], "limit": 500},
        headers=cases_api.headers(roles=["tenant_admin"]),
    ).json()
    actions = {e["action"] for e in entries}
    assert {"case.created", "case.version_created", "case.field_set", "case.item_added",
            "case.day_zero_set", "case.clock_started", "case.version_approved",
            "signature.applied"} <= actions  # fmt: skip
    field_set = next(e for e in entries if e["field"] == "/patient/initials")
    assert field_set["case_version"] == 1
    assert field_set["new_value"]["value"] == "JD"
    with db.tenant_session(cases_api.tenant) as session:
        assert verify_chain(session, cases_api.tenant).ok


def test_cases_are_isolated_between_tenants(
    cases_api: Any, op: Op, client: TestClient, issue_token: Callable[..., str], tenant_b: UUID
) -> None:
    case = cases_api.create([op("/patient/initials", "JD")])
    other = {
        "Authorization": f"Bearer {issue_token(tenant_id=str(tenant_b), roles=['case_processor'])}"
    }
    assert client.get(f"/cases/{case['id']}", headers=other).status_code == 404
    assert client.get(f"/cases/{case['id']}/versions/1", headers=other).status_code == 404
    assert client.get("/cases", headers=other).json() == []
    patch = {"expected_revision": 1, "changes": [op("/patient/initials", "XX")]}
    assert (
        client.patch(f"/cases/{case['id']}/versions/1", json=patch, headers=other).status_code
        == 404
    )


def test_data_scopes_limit_which_cases_a_user_sees(cases_api: Any, case_ops: Ops, op: Op) -> None:
    eu_acm = cases_api.create(case_ops(RECEIPT, country="DE", product="ACM-001"))
    us_acm = cases_api.create(case_ops(RECEIPT, country="US", product="ACM-001"))
    eu_glx = cases_api.create(case_ops(RECEIPT, country="FR", product="GLX-9"))
    unknown = cases_api.create([op("/patient/initials", "JD")])

    def visible(products: list[str], regions: list[str]) -> set[str]:
        headers = cases_api.headers(ps_products=products, ps_regions=regions)
        return {c["id"] for c in cases_api.client.get("/cases", headers=headers).json()}

    assert {eu_acm["id"], us_acm["id"], eu_glx["id"], unknown["id"]} <= visible(["*"], ["*"])
    assert visible(["ACM-001"], ["eu"]) == {eu_acm["id"]}
    assert visible(["ACM-001"], ["*"]) == {eu_acm["id"], us_acm["id"]}
    assert visible(["*"], ["eu"]) >= {eu_acm["id"], eu_glx["id"]}
    assert unknown["id"] not in visible(["*"], ["eu", "us"])  # region unknown: fail closed
    assert visible([], []) == set()  # no scope claims: nothing

    restricted = cases_api.headers(ps_products=["ACM-001"], ps_regions=["eu"])
    assert cases_api.client.get(f"/cases/{eu_glx['id']}", headers=restricted).status_code == 404


def test_roles_are_enforced(cases_api: Any, op: Op) -> None:
    for role in ("quality_reviewer", "medical_reviewer", "tenant_admin"):
        response = cases_api.client.post(
            "/cases", json={"changes": []}, headers=cases_api.headers(roles=[role])
        )
        assert response.status_code == 403, role
    case = cases_api.create([], roles=["intake_processor"])
    response = cases_api.edit(
        case["id"], 1, [op("/patient/initials", "JD")], roles=["intake_processor"]
    )
    assert response.status_code == 403
    platform = cases_api.client.get(
        "/cases", headers=cases_api.headers(roles=["platform_admin"], tenant_id=None)
    )
    assert platform.status_code == 403


def test_case_spans_carry_case_id(cases_api: Any, spans: InMemorySpanExporter) -> None:
    case = cases_api.create([])
    cases_api.version(case["id"])
    server = [s for s in spans.get_finished_spans() if s.name.startswith("GET /cases/{case_id}")]
    assert server
    assert all(s.attributes and s.attributes.get("case_id") == case["id"] for s in server)
    assert all(
        s.attributes and s.attributes.get("tenant_id") == str(cases_api.tenant) for s in server
    )


def test_day_zero_and_clocks_cannot_be_reset(
    cases_api: Any, case_ops: Ops, db: Database, owner_engine: Engine
) -> None:
    case = cases_api.create(case_ops(RECEIPT, serious=True))
    with pytest.raises(DBAPIError, match="day 0"), db.tenant_session(cases_api.tenant) as s:
        s.execute(
            text("UPDATE cases SET day_zero = :d WHERE id = :c"),
            {"d": date(2030, 1, 1), "c": case["id"]},
        )
    with pytest.raises(DBAPIError, match="append-only"), owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE case_clocks SET due_date = due_date + 30 WHERE case_id = :c"),
            {"c": case["id"]},
        )
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        db.tenant_session(cases_api.tenant) as s,
    ):
        s.execute(text("UPDATE cases SET case_number = 'X' WHERE id = :c"), {"c": case["id"]})
