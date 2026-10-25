"""Fixtures for AI pipeline tests: real intake, real database, Temporal's test server."""

import asyncio
import base64
import json
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, text
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from pharmasafe_ai_worker.activities import Deps, PipelineActivities
from pharmasafe_ai_worker.contracts import (
    PIPELINE_WORKFLOW,
    TASK_QUEUE,
    PipelineInput,
    PipelineSummary,
    pipeline_workflow_id,
)
from pharmasafe_ai_worker.workflows import CasePipelineWorkflow
from pharmasafe_api.cases import service as cases
from pharmasafe_api.cases.models import CaseRow
from pharmasafe_api.intake import service as intake
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.audit import Actor
from pharmasafe_platform.blobstore import InMemoryBlobStore
from pharmasafe_platform.db import Database


@dataclass
class Run:
    case_id: UUID
    summary: PipelineSummary
    # Every payload in the workflow history, decoded: what Temporal stores.
    history: str


def decoded_history(history_json: str) -> str:
    """The history with every base64 payload decoded, to search for personal data."""
    decoded: list[str] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "data" in node and isinstance(node["data"], str):
                try:
                    decoded.append(base64.b64decode(node["data"]).decode("utf-8", "replace"))
                except ValueError:
                    decoded.append(node["data"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(json.loads(history_json))
    return "\n".join(decoded)


@pytest.fixture
def ingest_report(db: Database) -> Callable[[UUID, str], UUID]:
    """Take a report in through the intake API service; returns the new case id."""
    deps = intake.IntakeDeps(InMemoryBlobStore(), None, seed_registry())

    def ingest(tenant: UUID, report: str) -> UUID:
        submission = intake.Submission(
            channel=intake.Channel.API,
            external_id=str(uuid4()),
            request_hash=intake.request_hash(report),
            documents=[
                intake.IncomingDocument(
                    part="text",
                    content_type="text/plain; charset=utf-8",
                    data=report.encode(),
                    text=report,
                )
            ],
        )
        with db.tenant_session(tenant) as session:
            result = intake.ingest(session, tenant, Actor("intake-1", "Intake"), submission, deps)
            return result.case_ids[0]

    return ingest


@pytest.fixture
def run_pipelines() -> Callable[[Deps, list[tuple[UUID, UUID]]], list[Run]]:
    """Run the pipeline workflow for each (tenant, case), in order, on one test server."""

    def run(deps: Deps, items: list[tuple[UUID, UUID]]) -> list[Run]:
        async def main() -> list[Run]:
            env = await WorkflowEnvironment.start_time_skipping()
            runs = []
            try:
                with ThreadPoolExecutor(max_workers=8) as executor:
                    worker = Worker(
                        env.client,
                        task_queue=TASK_QUEUE,
                        workflows=[CasePipelineWorkflow],
                        activities=PipelineActivities(deps).all(),
                        activity_executor=executor,
                    )
                    async with worker:
                        for tenant, case_id in items:
                            handle = await env.client.start_workflow(
                                PIPELINE_WORKFLOW,
                                PipelineInput(str(tenant), str(case_id), 1),
                                id=pipeline_workflow_id(str(case_id), 1),
                                task_queue=TASK_QUEUE,
                                result_type=PipelineSummary,
                            )
                            summary = await handle.result()
                            history = await handle.fetch_history()
                            runs.append(Run(case_id, summary, decoded_history(history.to_json())))
            finally:
                await env.shutdown()
            return runs

        return asyncio.run(main())

    return run


@pytest.fixture
def case_content(db: Database) -> Callable[[UUID, UUID], SafetyCase]:
    def read(tenant: UUID, case_id: UUID) -> SafetyCase:
        with db.tenant_session(tenant) as session:
            case = session.get_one(CaseRow, case_id)
            return cases.case_of(cases.get_version(session, case_id, case.latest_version))

    return read


@pytest.fixture
def set_automation(owner_engine: Engine) -> Callable[[UUID, str], None]:
    def set_level(tenant: UUID, level: str) -> None:
        with owner_engine.begin() as conn:
            conn.execute(
                text("UPDATE tenants SET ai_automation_level = :l WHERE id = :t"),
                {"l": level, "t": tenant},
            )

    return set_level
