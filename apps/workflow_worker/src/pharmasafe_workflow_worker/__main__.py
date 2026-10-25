"""Worker entrypoint: ``python -m pharmasafe_workflow_worker``.

Runs two things in one process:

- the Temporal worker for the case workflow and its activities;
- the outbox relay, which turns committed case events into workflow signals.

Several replicas can run side by side: Temporal shares the task queue between them, and
the relay claims outbox rows with ``SKIP LOCKED``.
"""

import argparse
import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from pharmasafe_platform.db import Database
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.outbox import OutboxRelay
from pharmasafe_platform.settings import get_settings
from pharmasafe_workflow_worker.activities import CaseActivities
from pharmasafe_workflow_worker.contracts import TASK_QUEUE
from pharmasafe_workflow_worker.publisher import TemporalPublisher
from pharmasafe_workflow_worker.workflows import CaseWorkflow

SERVICE_NAME = "workflow-worker"
log = logging.getLogger(SERVICE_NAME)


def build_worker(client: Client, database: Database, executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CaseWorkflow],
        activities=CaseActivities(database).all(),
        activity_executor=executor,
    )


async def relay_forever(relay: OutboxRelay, interval: float) -> None:
    while True:
        try:
            await asyncio.to_thread(relay.run_once)
        except Exception:  # keep relaying; the failed rows are retried next round
            log.exception("outbox relay round failed")
        await asyncio.sleep(interval)


async def run(relay_interval: float) -> None:
    settings = get_settings()
    database = Database(settings.database_url)
    client = await Client.connect(settings.temporal_address)
    relay = OutboxRelay(database, TemporalPublisher(client, asyncio.get_running_loop()))
    with ThreadPoolExecutor(max_workers=16) as executor:
        worker = build_worker(client, database, executor)
        log.info("workflow worker started", extra={"temporal_address": settings.temporal_address})
        await asyncio.gather(worker.run(), relay_forever(relay, relay_interval))


def main() -> None:
    parser = argparse.ArgumentParser(description="PharmaSafe case workflow worker")
    parser.add_argument(
        "--relay-interval", type=float, default=1.0, help="seconds between relay rounds"
    )
    args = parser.parse_args()
    configure_logging(SERVICE_NAME, get_settings().log_level)
    asyncio.run(run(args.relay_interval))


if __name__ == "__main__":
    main()
