"""The case workflow on Temporal's time-skipping test server, against the real database.

Covers the Phase 4 acceptance criteria: killing workers mid-case loses nothing and does
not reset clocks, and deadline escalations fire at the right (skipped-to) times.
"""

import asyncio
import json
import os
from collections.abc import Awaitable, Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from temporalio import activity
from temporalio.client import WorkflowHistory
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer

from pharmasafe_workflow_worker.activities import CaseActivities
from pharmasafe_workflow_worker.contracts import OpenTaskRequest
from pharmasafe_workflow_worker.workflows import WORKFLOW_VERSION, CaseWorkflow

HISTORIES = Path(__file__).parent / "histories"
TODAY = datetime.now(UTC).date()

Scenario = Callable[[Any, WorkflowEnvironment], Awaitable[None]]


def run(driver_factory: Any, scenario: Scenario) -> None:
    """Run an async scenario with a fresh time-skipping server (one event loop per test)."""

    async def main() -> None:
        env = await WorkflowEnvironment.start_time_skipping()
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                await scenario(driver_factory(env.client, executor), env)
        finally:
            await env.shutdown()

    asyncio.run(main())


# ---------------------------------------------------------------- lifecycle


def test_serious_case_runs_every_review_step_with_four_eyes(
    driver_factory: Any, make_content: Callable[..., Any]
) -> None:
    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        case_id = d.create_case(make_content(TODAY, serious=True))
        async with d.worker():
            await d.stage_becomes(case_id, "data_entry_review")
            d.complete(d.open_task(case_id))
            await d.stage_becomes(case_id, "quality_medical_review")

            quality = d.open_task(case_id)
            assert quality.kind == "quality_review"
            # The data entry reviewer cannot also do the quality review.
            with pytest.raises(Exception, match="issue"):
                d.complete(quality, "proc-1", "quality_reviewer")
            d.complete(quality)
            d.complete(await d.task_becomes(case_id, "medical_review"))
            await d.stage_becomes(case_id, "approval")
            d.complete(d.open_task(case_id))
            await d.stage_becomes(case_id, "approved")

            assert [k for k, s in d.tasks(case_id) if s == "completed"] == [
                "data_entry",
                "quality_review",
                "medical_review",
                "approval",
            ]
            state = await d.handle(case_id).query(CaseWorkflow.state)
            assert state.workflow_version == WORKFLOW_VERSION
            assert state.stage == "approved"

            # A follow-up starts the review of version 2 (the workflow continues as new).
            d.follow_up(case_id, TODAY)
            await d.stage_becomes(case_id, "data_entry_review")
            assert d.open_task(case_id).case_version == 2

    run(driver_factory, scenario)


def test_invalid_case_is_held_until_it_becomes_valid(
    driver_factory: Any, make_content: Callable[..., Any], no_qc_sampling: None
) -> None:
    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        case_id = d.create_case(make_content(TODAY, serious=False, valid=False))
        async with d.worker():
            await d.stage_becomes(case_id, "held_for_follow_up")
            d.edit(case_id, "/patient/initials", "JD")
            await d.stage_becomes(case_id, "data_entry_review")

    run(driver_factory, scenario)


def test_returned_or_edited_case_goes_back_to_data_entry(
    driver_factory: Any, make_content: Callable[..., Any], no_qc_sampling: None
) -> None:
    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        case_id = d.create_case(make_content(TODAY, serious=False))
        async with d.worker():
            await d.stage_becomes(case_id, "data_entry_review")
            d.complete(d.open_task(case_id))
            await d.stage_becomes(case_id, "approval")
            d.return_task(d.open_task(case_id), "patient sex is missing")
            await d.stage_becomes(case_id, "data_entry_review")

            d.complete(d.open_task(case_id))
            await d.stage_becomes(case_id, "approval")
            # An edit during approval voids the review: back to data entry.
            d.edit(case_id, "/patient/initials", "JK")
            await d.stage_becomes(case_id, "data_entry_review")
            assert [k for k, s in d.tasks(case_id)] == [
                "data_entry",
                "approval",
                "data_entry",
                "approval",
                "data_entry",
            ]

    run(driver_factory, scenario)


# ---------------------------------------------------------------- deadlines


def test_deadline_escalations_fire_before_and_after_the_due_date(
    driver_factory: Any, make_content: Callable[..., Any]
) -> None:
    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        start = await env.get_current_time()
        receipt = start.date()
        case_id = d.create_case(make_content(receipt, serious=True))  # FDA and EMA: 15 days
        async with d.worker():
            await d.stage_becomes(case_id, "data_entry_review")
            assert d.escalations(case_id) == []

            await env.sleep(
                timedelta(days=11)
                - (start - datetime(receipt.year, receipt.month, receipt.day, tzinfo=UTC))
            )
            await d.escalations_reach(case_id, 2)
            assert {level for _, _, level in d.escalations(case_id)} == {"warning"}

            await env.sleep(timedelta(days=3))  # day 14: two days before the due date
            await d.escalations_reach(case_id, 4)
            await env.sleep(timedelta(days=3))  # day 17: past the due date
            await d.escalations_reach(case_id, 6)
            levels = sorted(level for _, _, level in d.escalations(case_id))
            assert levels == [
                "escalation",
                "escalation",
                "overdue",
                "overdue",
                "warning",
                "warning",
            ]
            assert d.stage(case_id) == "data_entry_review"  # escalation does not move the case

    run(driver_factory, scenario)


# ---------------------------------------------------------------- crashes


def test_killing_the_worker_mid_case_loses_nothing_and_keeps_the_clocks(
    driver_factory: Any, make_content: Callable[..., Any]
) -> None:
    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        start = await env.get_current_time()
        case_id = d.create_case(make_content(start.date(), serious=True))
        # No workflow cache, so no sticky queue: the test server does not time out a dead
        # worker's sticky queue the way a real Temporal cluster does (after 5 s).
        async with d.worker(cache=False):
            await d.stage_becomes(case_id, "data_entry_review")
        clocks = d.clocks(case_id)
        assert clocks

        # The worker is gone. Work goes on: a person completes the task, and the event is
        # delivered while nobody is running the workflow.
        d.complete(d.open_task(case_id))
        await d.relay()

        async with d.worker(cache=False):  # a new worker picks the case up where it was
            await d.stage_becomes(case_id, "quality_medical_review")
            # The deadline timers set before the crash still fire. (The test server only
            # skips time while a worker is polling.)
            await env.sleep(timedelta(days=11))
            await d.escalations_reach(case_id, 2)

        assert d.clocks(case_id) == clocks  # nothing reset or recomputed
        assert [k for k, _ in d.tasks(case_id)] == ["data_entry", "quality_review"]
        assert sorted(level for _, _, level in d.escalations(case_id)) == ["warning", "warning"]

    run(driver_factory, scenario)


def test_an_activity_that_crashes_after_committing_is_retried_without_duplicates(
    driver_factory: Any, make_content: Callable[..., Any], db: Any
) -> None:
    activities = CaseActivities(db)
    crashed: list[str] = []

    @activity.defn(name="open_task")
    def open_task_then_crash(request: OpenTaskRequest) -> None:
        activities.open_task(request)
        if not crashed:  # the work is committed, then the worker "dies" before reporting
            crashed.append(request.task_id)
            raise RuntimeError("worker died after committing")

    flaky = [a for a in activities.all() if a.__name__ != "open_task"] + [open_task_then_crash]

    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        case_id = d.create_case(make_content(TODAY, serious=False))
        async with d.worker(flaky):
            await d.stage_becomes(case_id, "data_entry_review")
            for _ in range(20):
                if crashed and d.tasks(case_id):
                    break
                await asyncio.sleep(0.2)
        assert crashed
        assert d.tasks(case_id) == [("data_entry", "open")]

    run(driver_factory, scenario)


# ---------------------------------------------------------------- versioning


def test_recorded_histories_still_replay(
    driver_factory: Any, make_content: Callable[..., Any], no_qc_sampling: None
) -> None:
    """Workflow versioning guard: today's code must replay every recorded history.

    Set ``PHARMASAFE_RECORD_HISTORIES=1`` to record a new history after a deliberate,
    patched workflow change; commit the file with the change.
    """

    async def scenario(d: Any, env: WorkflowEnvironment) -> None:
        case_id = d.create_case(make_content(TODAY, serious=False))
        async with d.worker():
            await d.stage_becomes(case_id, "data_entry_review")
            d.complete(d.open_task(case_id))
            await d.stage_becomes(case_id, "approval")
            history = await d.handle(case_id).fetch_history()

        replayer = Replayer(workflows=[CaseWorkflow])
        await replayer.replay_workflow(history)  # the history this code just produced

        HISTORIES.mkdir(exist_ok=True)
        recorded = HISTORIES / f"case_workflow_v{WORKFLOW_VERSION}.json"
        if os.environ.get("PHARMASAFE_RECORD_HISTORIES") or not recorded.exists():
            recorded.write_text(
                json.dumps(json.loads(history.to_json()), indent=1), encoding="utf-8"
            )
        for path in sorted(HISTORIES.glob("*.json")):
            await replayer.replay_workflow(
                WorkflowHistory.from_json(path.stem, path.read_text(encoding="utf-8"))
            )

    run(driver_factory, scenario)
