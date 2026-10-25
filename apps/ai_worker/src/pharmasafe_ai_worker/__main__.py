"""Worker entrypoint: ``python -m pharmasafe_ai_worker``.

Runs the AI pipeline workflow and its activities on the ``ai-pipeline`` task queue.
Model calls go to the AI gateway over HTTP; this worker never talks to a provider.
"""

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from temporalio.client import Client
from temporalio.worker import Worker

from pharmasafe_ai_gateway.client import HttpGateway
from pharmasafe_ai_worker.activities import Deps, PipelineActivities
from pharmasafe_ai_worker.contracts import TASK_QUEUE
from pharmasafe_ai_worker.workflows import CasePipelineWorkflow
from pharmasafe_api.coding.dictionary import SyntheticDictionary
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.aiconfig import BundleStore
from pharmasafe_platform.db import Database
from pharmasafe_platform.encryption import TenantCipher
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.settings import Settings, get_settings

SERVICE_NAME = "ai-worker"
log = logging.getLogger(SERVICE_NAME)


def build_deps(settings: Settings) -> Deps:
    master_key = settings.redaction_master_key.get_secret_value()
    if not master_key:
        raise SystemExit("PHARMASAFE_REDACTION_MASTER_KEY is not set")
    return Deps(
        database=Database(settings.database_url),
        gateway=HttpGateway(
            httpx.Client(), settings.ai_gateway_url, settings.ai_gateway_token.get_secret_value()
        ),
        bundles=BundleStore(Path(settings.ai_bundle_dir)),
        bundle_version=settings.ai_bundle_version,
        cipher=TenantCipher.from_base64(settings.redaction_key_id, master_key),
        dictionary=SyntheticDictionary(),
        registry=seed_registry(),
    )


def build_worker(client: Client, deps: Deps, executor: ThreadPoolExecutor) -> Worker:
    return Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[CasePipelineWorkflow],
        activities=PipelineActivities(deps).all(),
        activity_executor=executor,
    )


async def run() -> None:
    settings = get_settings()
    deps = build_deps(settings)
    client = await Client.connect(settings.temporal_address)
    with ThreadPoolExecutor(max_workers=16) as executor:
        log.info("ai worker started", extra={"bundle_version": deps.bundle_version})
        await build_worker(client, deps, executor).run()


def main() -> None:
    configure_logging(SERVICE_NAME, get_settings().log_level)
    asyncio.run(run())


if __name__ == "__main__":
    main()
