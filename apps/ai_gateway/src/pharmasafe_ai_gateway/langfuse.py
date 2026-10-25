"""LLM call logging to self-hosted Langfuse, through its public ingestion API.

One Langfuse trace per case (``case-{case_id}``), one generation per model call, with:
prompt version, bundle version, model, provider, token counts, latency, cost and outcome.

Only redacted content is ever sent: the gateway refuses requests containing detectable
personal data before they are logged or forwarded. Logging is best effort and never
blocks or fails a model call: events are sent from a background thread, and failures
are logged and dropped (the ``ai_calls`` table remains the system of record).
"""

import logging
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import httpx

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Generation:
    tenant_id: str
    case_id: str
    step: str
    bundle_version: str
    prompt_version: str
    provider: str
    model: str
    system: str
    user: str
    output: str | None
    input_tokens: int
    output_tokens: int
    cost_usd: float
    started_at: datetime
    ended_at: datetime
    outcome: str


class CallLog(Protocol):
    def record(self, generation: Generation) -> None: ...


class NoCallLog:
    def record(self, generation: Generation) -> None:
        return None


class LangfuseLog:
    def __init__(self, client: httpx.Client, host: str, public_key: str, secret_key: str) -> None:
        self._client = client
        self._url = f"{host.rstrip('/')}/api/public/ingestion"
        self._auth = (public_key, secret_key)
        self._executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="langfuse")
        self._pending: list[Future[None]] = []

    def record(self, generation: Generation) -> None:
        self._pending = [f for f in self._pending if not f.done()]
        self._pending.append(self._executor.submit(self._send, generation))

    def flush(self, timeout: float = 10) -> None:
        for future in list(self._pending):
            future.result(timeout=timeout)

    def _send(self, g: Generation) -> None:
        try:
            response = self._client.post(
                self._url, json={"batch": events(g)}, auth=self._auth, timeout=5
            )
            if response.status_code >= 300:
                log.warning("langfuse rejected events", extra={"status": response.status_code})
        except httpx.HTTPError as exc:
            log.warning("langfuse unreachable", extra={"error": type(exc).__name__})


def events(g: Generation) -> list[dict[str, Any]]:
    trace_id = f"case-{g.case_id}"
    now = g.ended_at.isoformat()
    metadata = {
        "tenant_id": g.tenant_id,
        "bundle_version": g.bundle_version,
        "prompt_version": g.prompt_version,
        "provider": g.provider,
        "outcome": g.outcome,
        "cost_usd": g.cost_usd,
    }
    return [
        {
            "id": str(uuid.uuid4()),
            "timestamp": now,
            "type": "trace-create",
            "body": {
                "id": trace_id,
                "name": "case",
                "metadata": {"tenant_id": g.tenant_id, "case_id": g.case_id},
            },
        },
        {
            "id": str(uuid.uuid4()),
            "timestamp": now,
            "type": "generation-create",
            "body": {
                "id": str(uuid.uuid4()),
                "traceId": trace_id,
                "name": g.step,
                "model": g.model,
                "startTime": g.started_at.isoformat(),
                "endTime": now,
                "input": [
                    {"role": "system", "content": g.system},
                    {"role": "user", "content": g.user},
                ],
                "output": g.output,
                "usage": {"input": g.input_tokens, "output": g.output_tokens, "unit": "TOKENS"},
                "metadata": metadata,
                "level": "DEFAULT" if g.outcome == "ok" else "ERROR",
            },
        },
    ]
