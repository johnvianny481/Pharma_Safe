"""Review tasks: four-eyes, review order, edits voiding reviews, queues and assignment."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError

from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.review import service as review

Ops = Callable[..., list[dict[str, Any]]]
Op = Callable[[str, Any], dict[str, Any]]
RECEIPT = datetime.now(UTC).date() - timedelta(days=3)


def _sample_rate(owner_engine: Engine, tenant: UUID, rate: str) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE tenants SET qc_sample_rate = :r WHERE id = :t"), {"r": rate, "t": tenant}
        )


def _issue_codes(response: Any) -> set[str]:
    return {i["code"] for i in response.json()["detail"]["issues"]}


def test_four_eyes_the_data_entry_reviewer_cannot_approve(
    cases_api: Any, case_ops: Ops, owner_engine: Engine
) -> None:
    _sample_rate(owner_engine, cases_api.tenant, "0")  # no QC sampling: data entry, then approval
    case = cases_api.create(case_ops(RECEIPT))
    data_entry = cases_api.open_task(case["id"], 1, "data_entry")
    assert cases_api.complete_task(data_entry, "proc-1", "case_processor").status_code == 200

    approval = cases_api.open_task(case["id"], 1, "approval")
    # The same person, even holding an approver role, is refused.
    same_person = cases_api.complete_task(approval, "proc-1", "quality_reviewer")
    assert same_person.status_code == 422
    assert _issue_codes(same_person) == {"four_eyes"}
    assert cases_api.complete_task(approval, "qa-1", "quality_reviewer").status_code == 200
    assert cases_api.version(case["id"])["status"] == "approved"


def test_approval_needs_the_data_entry_review_first(cases_api: Any, case_ops: Ops) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    approval = cases_api.open_task(case["id"], 1, "approval")
    response = cases_api.complete_task(approval, "qa-1", "quality_reviewer")
    assert response.status_code == 422
    assert "review_missing" in _issue_codes(response)


def test_sampled_non_serious_case_needs_a_quality_review(
    cases_api: Any, case_ops: Ops, owner_engine: Engine
) -> None:
    _sample_rate(owner_engine, cases_api.tenant, "1")  # every non-serious case is sampled
    case = cases_api.create(case_ops(RECEIPT))
    cases_api.complete_task(
        cases_api.open_task(case["id"], 1, "data_entry"), "proc-1", "case_processor"
    )
    response = cases_api.complete_task(
        cases_api.open_task(case["id"], 1, "approval"), "qa-1", "quality_reviewer"
    )
    assert _issue_codes(response) == {"quality_review_missing"}


def test_serious_case_needs_quality_and_medical_review(cases_api: Any, case_ops: Ops) -> None:
    case = cases_api.create(case_ops(RECEIPT, serious=True))
    cases_api.complete_task(
        cases_api.open_task(case["id"], 1, "data_entry"), "proc-1", "case_processor"
    )
    approval = cases_api.open_task(case["id"], 1, "approval")
    response = cases_api.complete_task(approval, "qa-1", "quality_reviewer")
    assert _issue_codes(response) == {"quality_review_missing", "medical_review_missing"}

    quality = cases_api.open_task(case["id"], 1, "quality_review")
    assert cases_api.complete_task(quality, "qa-2", "quality_reviewer").status_code == 200
    medical = cases_api.open_task(case["id"], 1, "medical_review")
    # Medical review needs the medical role.
    assert cases_api.complete_task(medical, "qa-2", "quality_reviewer").status_code == 403
    assert cases_api.complete_task(medical, "doc-1", "medical_reviewer").status_code == 200
    approval = cases_api.open_task(case["id"], 1, "approval")
    assert cases_api.complete_task(approval, "qa-1", "quality_reviewer").status_code == 200


def test_an_edit_after_review_voids_the_review(cases_api: Any, case_ops: Ops, op: Op) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    cases_api.complete_review(case["id"])
    assert cases_api.edit(case["id"], 1, [op("/patient/initials", "ZZ")]).status_code == 200
    response = cases_api.approve(case["id"])
    assert response.status_code == 422
    assert "review_missing" in _issue_codes(response)


def test_opening_a_task_cancels_the_previous_one_and_is_idempotent(
    cases_api: Any, op: Op, db: Any
) -> None:
    case = cases_api.create([op("/patient/initials", "JD")])
    first = cases_api.open_task(case["id"], 1, "data_entry")
    second = cases_api.open_task(case["id"], 1, "approval")
    tasks = {
        t["id"]: t
        for t in cases_api.client.get(
            "/tasks", headers=cases_api.headers(roles=["quality_reviewer", "case_processor"])
        ).json()
    }
    assert second in tasks
    assert first not in tasks  # cancelled

    # Re-running the activity with the same id returns the same task.
    with db.tenant_session(cases_api.tenant) as session:
        case_row = session.get_one(CaseRow, UUID(case["id"]))
        again = review.open_task(
            session, case_row, 1, review.Kind.APPROVAL, UUID(second), datetime.now(UTC)
        )
        assert str(again.id) == second
        assert len(review.open_tasks_of(session, case_row.id)) == 1


def test_queue_is_ordered_by_deadline_risk_and_filtered_by_role(
    cases_api: Any, case_ops: Ops, op: Op
) -> None:
    later = cases_api.create(case_ops(RECEIPT, serious=True))  # due in 15 days from receipt
    sooner = cases_api.create(case_ops(RECEIPT - timedelta(days=10), serious=True))
    no_deadline = cases_api.create([op("/patient/initials", "JD")])
    ids = {}
    for name, case in (("later", later), ("sooner", sooner), ("none", no_deadline)):
        ids[cases_api.open_task(case["id"], 1, "data_entry")] = name

    queue = cases_api.client.get(
        "/tasks", headers=cases_api.headers(roles=["case_processor"])
    ).json()
    order = [ids[t["id"]] for t in queue if t["id"] in ids]
    assert order == ["sooner", "later", "none"]
    assert queue[0]["next_due"] is not None
    medical_queue = cases_api.client.get(
        "/tasks", headers=cases_api.headers(roles=["medical_reviewer"])
    ).json()
    assert all(t["kind"] in ("medical_review", "approval") for t in medical_queue)


def test_claim_assign_and_only_the_assignee_completes(cases_api: Any, case_ops: Ops) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    task = cases_api.open_task(case["id"], 1, "data_entry")
    proc1 = cases_api.headers(roles=["case_processor"], sub="proc-1")
    proc2 = cases_api.headers(roles=["case_processor"], sub="proc-2")
    assert (
        cases_api.client.post(f"/tasks/{task}/claim", headers=proc1).json()["assignee"] == "proc-1"
    )
    assert cases_api.client.post(f"/tasks/{task}/claim", headers=proc2).status_code == 409
    assert cases_api.complete_task(task, "proc-2", "case_processor").status_code == 409
    mine = cases_api.client.get("/tasks", params={"mine": True}, headers=proc1).json()
    assert [t["id"] for t in mine] == [task]

    # Only tenant admins assign; they can hand the task over.
    assert (
        cases_api.client.post(
            f"/tasks/{task}/assign", json={"assignee": "proc-2"}, headers=proc1
        ).status_code
        == 403
    )
    admin = cases_api.headers(roles=["tenant_admin"])
    assert (
        cases_api.client.post(
            f"/tasks/{task}/assign", json={"assignee": "proc-2"}, headers=admin
        ).status_code
        == 200
    )
    assert cases_api.complete_task(task, "proc-2", "case_processor").status_code == 200


def test_reviewer_can_return_a_case_to_data_entry(cases_api: Any, case_ops: Ops) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    data_entry = cases_api.open_task(case["id"], 1, "data_entry")
    qa = cases_api.headers(roles=["quality_reviewer"], sub="qa-1")
    processor = cases_api.headers(roles=["case_processor"])
    returned = cases_api.client.post(
        f"/tasks/{data_entry}/return", json={"reason": "x"}, headers=processor
    )
    assert returned.status_code == 409  # data entry itself cannot be returned

    cases_api.complete_task(data_entry, "proc-1", "case_processor")
    approval = cases_api.open_task(case["id"], 1, "approval")
    response = cases_api.client.post(
        f"/tasks/{approval}/return", json={"reason": "dose unit missing"}, headers=qa
    )
    assert response.status_code == 200
    assert (response.json()["status"], response.json()["close_reason"]) == (
        "returned",
        "dose unit missing",
    )
    assert (
        cases_api.complete_task(approval, "qa-1", "quality_reviewer").status_code == 409
    )  # closed


def test_tasks_are_isolated_by_tenant_and_scope(
    cases_api: Any, case_ops: Ops, issue_token: Callable[..., str], tenant_b: Any
) -> None:
    case = cases_api.create(case_ops(RECEIPT, country="US"))
    task = cases_api.open_task(case["id"], 1, "data_entry")
    other = {
        "Authorization": f"Bearer {issue_token(tenant_id=str(tenant_b), roles=['case_processor'])}"
    }
    assert task not in [t["id"] for t in cases_api.client.get("/tasks", headers=other).json()]
    assert cases_api.client.post(f"/tasks/{task}/claim", headers=other).status_code == 404
    eu_only = cases_api.headers(ps_products=["ACM-001"], ps_regions=["eu"])
    assert task not in [t["id"] for t in cases_api.client.get("/tasks", headers=eu_only).json()]
    assert cases_api.client.post(f"/tasks/{task}/claim", headers=eu_only).status_code == 404


def test_closed_tasks_cannot_change(cases_api: Any, case_ops: Ops, owner_engine: Engine) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    task = cases_api.open_task(case["id"], 1, "data_entry")
    cases_api.complete_task(task, "proc-1", "case_processor")
    with pytest.raises(DBAPIError, match="closed review tasks"), owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE review_tasks SET status = 'open', closed_at = NULL WHERE id = :t"),
            {"t": task},
        )
    with pytest.raises(DBAPIError, match="never deleted"), owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM review_tasks WHERE id = :t"), {"t": task})


def test_task_completion_is_announced_to_the_workflow(
    cases_api: Any, case_ops: Ops, owner_engine: Engine
) -> None:
    case = cases_api.create(case_ops(RECEIPT))
    task = cases_api.open_task(case["id"], 1, "data_entry")
    cases_api.complete_task(task, "proc-1", "case_processor")
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT event_type, payload->>'task_id' FROM outbox WHERE aggregate_id = :c ORDER BY seq"
            ),
            {"c": case["id"]},
        ).all()
    assert ("task.completed", task) in [tuple(r) for r in rows]
    assert "case.updated" in [r[0] for r in rows]
