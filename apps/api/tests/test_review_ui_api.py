"""APIs added for the review UI: duplicate merge, source text, coding search, tenant
settings and the enriched work queue."""

from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_api.cases import ai as case_ai
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.review.models import ReviewTaskRow
from pharmasafe_domain.ai.automation import Level
from pharmasafe_platform.db import Database

Ops = Callable[..., list[dict[str, Any]]]
RECEIPT = date(2026, 10, 1)


def _merge(cases_api: Any, duplicate: str, master: str, picks: list[str], **who: Any) -> Any:
    sub, role = who.get("sub", "proc-2"), who.get("role", "case_processor")
    reauth = cases_api.issue_token(sub=sub, tenant_id=str(cases_api.tenant), roles=[role])
    return cases_api.client.post(
        f"/cases/{duplicate}/merge",
        json={"into": master, "picks": picks, "reauth_token": reauth},
        headers=cases_api.headers(roles=[role], sub=sub),
    )


# ---------------------------------------------------------------- merge


def test_merge_copies_picked_values_signs_and_closes_the_duplicate(
    cases_api: Any, case_ops: Ops, op: Callable[..., dict[str, Any]], db: Database
) -> None:
    master = cases_api.create(case_ops(RECEIPT))
    duplicate = cases_api.create(
        [
            op("/patient/initials", "JD"),
            op("/patient/weight_kg", "81"),
            {"op": "add", "path": "/reactions", "id": "r1"},
            op("/reactions/r1/reported_term", "headache"),
        ]
    )
    task = cases_api.open_task(duplicate["id"], 1, "data_entry")
    response = _merge(
        cases_api, duplicate["id"], master["id"], ["/patient/weight_kg", "/reactions/r1"]
    )
    assert response.status_code == 200, response.text
    content = response.json()["content"]
    weight = content["patient"]["weight_kg"]
    assert (weight["value"], weight["source"], weight["confirmed_by"]) == ("81", "import", None)
    terms = {r["id"]: r["reported_term"]["value"] for r in content["reactions"]}
    assert terms == {"r1": "rash", "m1-r1": "headache"}  # the clashing id was renamed

    detail = cases_api.client.get(f"/cases/{duplicate['id']}", headers=cases_api.headers())
    assert detail.json()["merged_into"] == master["id"]
    with db.tenant_session(cases_api.tenant) as session:
        assert session.get_one(ReviewTaskRow, UUID(task)).status == "cancelled"
        meanings = session.scalars(
            text("SELECT meaning FROM e_signatures WHERE entity_id = :c"), {"c": duplicate["id"]}
        ).all()
        actions = session.scalars(
            text("SELECT action FROM audit_ledger WHERE action LIKE 'case.merge%'")
        ).all()
        event = session.scalars(
            text("SELECT event_type FROM outbox WHERE aggregate_id = :c ORDER BY seq DESC"),
            {"c": duplicate["id"]},
        ).first()
    assert meanings == ["Duplicate merged"]
    assert set(actions) == {"case.merged", "case.merge_received"}
    assert event == "case.merged"


def test_merging_into_an_approved_case_starts_a_follow_up(
    cases_api: Any, case_ops: Ops, op: Callable[..., dict[str, Any]]
) -> None:
    master = cases_api.create(case_ops(RECEIPT))
    cases_api.complete_review(master["id"])
    assert cases_api.approve(master["id"]).status_code == 200
    duplicate = cases_api.create([op("/patient/weight_kg", "70")])
    response = _merge(cases_api, duplicate["id"], master["id"], ["/patient/weight_kg"])
    assert response.status_code == 200, response.text
    assert (response.json()["version"], response.json()["status"]) == (2, "draft")
    assert cases_api.version(master["id"], 1)["content"]["patient"]["weight_kg"] is None


def test_a_merge_is_final_and_checked(
    cases_api: Any,
    case_ops: Ops,
    op: Callable[..., dict[str, Any]],
    owner_engine: Engine,
) -> None:
    master = cases_api.create(case_ops(RECEIPT))
    other = cases_api.create([])
    duplicate = cases_api.create([op("/patient/weight_kg", "70")])
    assert _merge(cases_api, duplicate["id"], duplicate["id"], []).status_code == 409
    assert _merge(cases_api, duplicate["id"], master["id"], ["/patient/age"]).status_code == 422
    assert _merge(cases_api, duplicate["id"], master["id"], ["/causality/c1"]).status_code == 422
    assert _merge(cases_api, duplicate["id"], master["id"], [], role="intake_processor",
                  sub="in-1").status_code == 403  # fmt: skip
    assert _merge(cases_api, duplicate["id"], master["id"], []).status_code == 200
    again = _merge(cases_api, duplicate["id"], other["id"], [])
    assert again.status_code == 409
    with pytest.raises(DBAPIError, match="a merge is final"), owner_engine.begin() as conn:
        conn.execute(text("UPDATE cases SET merged_into = NULL, merged_at = NULL,"
                          " merged_by = NULL WHERE id = :c"), {"c": duplicate["id"]})  # fmt: skip


def test_an_approved_case_cannot_be_merged_away(cases_api: Any, case_ops: Ops) -> None:
    approved = cases_api.create(case_ops(RECEIPT))
    cases_api.complete_review(approved["id"])
    cases_api.approve(approved["id"])
    master = cases_api.create([])
    assert _merge(cases_api, approved["id"], master["id"], []).status_code == 409


# ---------------------------------------------------------------- source, coding


def test_the_source_text_of_a_case_from_intake(intake_client: Any, issue_token: Any,
                                               tenant_a: UUID) -> None:  # fmt: skip
    report = "Patient JD developed a rash after Acmezol."
    headers = {
        "Authorization": f"Bearer {issue_token(tenant_id=str(tenant_a), roles=['case_processor'])}"
    }
    ack = intake_client.post(
        "/intake/reports",
        json={"text": report},
        headers=headers | {"Idempotency-Key": "source-test-1"},
    )
    assert ack.status_code == 201, ack.text
    ack = ack.json()
    case_id = ack["reports"][0]["case_id"]
    source = intake_client.get(f"/cases/{case_id}/source", headers=headers)
    assert source.status_code == 200, source.text
    body = source.json()
    assert body["text"][body["report_start"] : body["report_end"]] == report
    empty = intake_client.post("/cases", json={"changes": []}, headers=headers).json()
    assert intake_client.get(f"/cases/{empty['id']}/source", headers=headers).status_code == 404


def test_coding_search_returns_dictionary_candidates(cases_api: Any) -> None:
    response = cases_api.client.get(
        "/coding/meddra_llt/search", params={"term": "hives"}, headers=cases_api.headers()
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["release"] == "SYNTH-MEDDRA-1.0"
    assert body["candidates"][0]["label"] == "Urticaria"


# ---------------------------------------------------------------- tenant admin


def _admin(cases_api: Any, **claims: Any) -> dict[str, str]:
    headers: dict[str, str] = cases_api.headers(roles=["tenant_admin"], sub="adm-1", **claims)
    return headers


def test_a_tenant_admin_changes_their_settings_with_an_audit_trail(
    cases_api: Any, db: Database, tenant_b: UUID
) -> None:
    before = cases_api.client.get("/admin/tenant", headers=_admin(cases_api)).json()
    assert before["available_regulators"] == ["CDSCO", "EMA", "FDA"]
    assert before["ai_monthly_budget_usd"] == "500"
    response = cases_api.client.patch(
        "/admin/tenant",
        json={"ai_automation_level": "L0", "qc_sample_rate": "0.25",
              "regulators": ["fda", "CDSCO"], "ai_monthly_budget_usd": "120.50",
              "reason": "go-live settings"},
        headers=_admin(cases_api),
    )  # fmt: skip
    assert response.status_code == 200, response.text
    after = response.json()
    assert (after["ai_automation_level"], after["qc_sample_rate"]) == ("L0", "0.250")
    assert (after["regulators"], after["ai_monthly_budget_usd"]) == (["FDA", "CDSCO"], "120.50")
    with db.tenant_session(cases_api.tenant) as session:
        fields = session.scalars(
            text("SELECT field FROM audit_ledger WHERE action = 'tenant.settings_changed'")
        ).all()
    assert set(fields) == {"ai_automation_level", "qc_sample_rate", "regulators",
                           "ai_monthly_budget_usd"}  # fmt: skip
    # Another tenant's settings are untouched.
    other = cases_api.client.get(
        "/admin/tenant", headers=_admin(cases_api, tenant_id=str(tenant_b))
    )
    assert other.json()["ai_automation_level"] == "L1"


@pytest.mark.parametrize(
    "change",
    [
        {"ai_automation_level": "L3"},
        {"qc_sample_rate": "1.5"},
        {"regulators": ["MHRA"]},
        {"regulators": []},
        {"intake_mailbox": "not-an-email"},
        {"ai_monthly_budget_usd": "-1"},
    ],
)
def test_invalid_settings_are_refused(cases_api: Any, change: dict[str, Any]) -> None:
    response = cases_api.client.patch("/admin/tenant", json=change, headers=_admin(cases_api))
    assert response.status_code == 422


def test_only_tenant_admins_and_only_their_own_row(cases_api: Any, db: Database,
                                                   tenant_b: UUID) -> None:  # fmt: skip
    response = cases_api.client.get("/admin/tenant", headers=cases_api.headers())
    assert response.status_code == 403
    with db.tenant_session(cases_api.tenant) as session:
        changed = session.execute(
            text("UPDATE tenants SET ai_automation_level = 'L0' WHERE id = :t"), {"t": tenant_b}
        ).rowcount
    assert changed == 0  # row-level security hides other tenants' rows from updates
    with (
        pytest.raises(DBAPIError, match="permission denied"),
        db.tenant_session(cases_api.tenant) as session,
    ):
        session.execute(text("UPDATE tenants SET region = 'us' WHERE id = :t"),
                        {"t": cases_api.tenant})  # fmt: skip


# ---------------------------------------------------------------- queue


def test_the_queue_shows_stage_and_ai_flags(cases_api: Any, db: Database) -> None:
    case = cases_api.create([])
    cases_api.open_task(case["id"], 1, "data_entry")
    with db.tenant_session(cases_api.tenant) as session:
        row = session.get_one(CaseRow, UUID(case["id"]))
        case_ai.record_run(
            session, row, 1, kind=case_ai.PIPELINE, bundle_version="b", status="completed",
            level=Level.L1, flags=["serious_by_signals", "duplicate_candidates"], result={},
            now=datetime.now(UTC),
        )  # fmt: skip
    queue = cases_api.client.get("/tasks", headers=cases_api.headers()).json()
    (item,) = [t for t in queue if t["case_id"] == case["id"]]
    assert item["case_stage"] == "received"
    assert item["flags"] == ["serious_by_signals", "duplicate_candidates"]
