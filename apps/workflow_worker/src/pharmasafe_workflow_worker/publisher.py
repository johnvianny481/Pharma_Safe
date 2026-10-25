"""Outbox events to case workflows.

The outbox relay calls :meth:`TemporalPublisher.publish` for every committed event.
Case events become a *signal-with-start* to ``case-{case_id}``: the first event for a
case starts its workflow, later ones signal it. Because the relay delivers at least
once, the signal carries the outbox event id and the workflow ignores repeats.

Events for other consumers (``intake``, ``notifications`` topics) are not for the
workflow. Until the Service Bus publisher exists (ADR 0011), they are acknowledged here
without further delivery.
"""

import asyncio
import logging

from temporalio.client import Client

from pharmasafe_platform.outbox import OutboxMessage
from pharmasafe_workflow_worker.contracts import (
    SIGNAL,
    TASK_QUEUE,
    WORKFLOW_NAME,
    CaseEvent,
    CaseWorkflowInput,
    workflow_id,
)

log = logging.getLogger(__name__)

WORKFLOW_TOPIC = "cases"


def to_signal(message: OutboxMessage) -> tuple[str, CaseWorkflowInput, CaseEvent] | None:
    """The workflow id, start input and signal for a message, or None if not a case event."""
    if message.topic != WORKFLOW_TOPIC:
        return None
    payload = message.payload
    case_id = str(payload["case_id"])
    version = payload.get("version")
    event = CaseEvent(
        event_id=str(message.id),
        type=message.event_type,
        version=int(version) if version is not None else None,
        task_id=str(payload["task_id"]) if payload.get("task_id") else None,
        kind=str(payload["kind"]) if payload.get("kind") else None,
    )
    return workflow_id(case_id), CaseWorkflowInput(str(message.tenant_id), case_id), event


class TemporalPublisher:
    """Publisher for the outbox relay, which runs in a worker thread.

    Temporal's client is async; calls are handed to the worker's event loop and awaited.
    """

    def __init__(
        self, client: Client, loop: asyncio.AbstractEventLoop, *, timeout: float = 30
    ) -> None:
        self._client = client
        self._loop = loop
        self._timeout = timeout

    def publish(self, message: OutboxMessage) -> None:
        signal = to_signal(message)
        if signal is None:
            return
        future = asyncio.run_coroutine_threadsafe(self._signal_with_start(*signal), self._loop)
        future.result(timeout=self._timeout)

    async def _signal_with_start(
        self, wf_id: str, start: CaseWorkflowInput, event: CaseEvent
    ) -> None:
        await self._client.start_workflow(
            WORKFLOW_NAME,
            start,
            id=wf_id,
            task_queue=TASK_QUEUE,
            start_signal=SIGNAL,
            start_signal_args=[event],
        )
