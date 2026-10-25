"""Client for the AI gateway, used by the AI worker.

:class:`HttpGateway` has the same ``run_step`` interface as
:class:`~pharmasafe_ai_gateway.service.GatewayService`, and raises the same
:class:`~pharmasafe_ai_gateway.service.GatewayError`, so tests can run the pipeline
against the service in process and production goes over HTTP.
"""

from typing import Protocol

import httpx

from pharmasafe_ai_gateway.service import GatewayError, StepCall, StepResult

# Gateway refusals after which the pipeline falls back to L0 manual mode.
UNAVAILABLE = frozenset({"providers_unavailable", "budget_exceeded"})
# The gateway itself could not be reached: worth retrying before giving up.
UNREACHABLE = "gateway_unreachable"


class Gateway(Protocol):
    def run_step(self, call: StepCall) -> StepResult: ...


class HttpGateway:
    # Longer than the worst gateway call (3 attempts x 60 s on 2 providers, plus backoff,
    # about 365 s) and shorter than the activity timeout (600 s): a slow outage is reported
    # by the gateway, not mistaken for an unreachable one and paid for twice.
    def __init__(self, client: httpx.Client, base_url: str, token: str, timeout: float = 540):
        self._client = client
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout

    def run_step(self, call: StepCall) -> StepResult:
        try:
            response = self._client.post(
                f"{self._base}/v1/steps/{call.step.value}",
                json={
                    "case_id": call.case_id,
                    "bundle_version": call.bundle_version,
                    "variables": call.variables,
                },
                headers={
                    "Authorization": f"Bearer {self._token}",
                    "X-Tenant-Id": str(call.tenant_id),
                },
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            raise GatewayError(
                503, UNREACHABLE, f"gateway unreachable: {type(exc).__name__}"
            ) from exc
        if response.status_code == 200:
            data = response.json()
            return StepResult(**data)
        try:
            error = response.json()
            code, message = str(error["code"]), str(error["message"])
        except (ValueError, KeyError, TypeError):
            code, message = "gateway_error", f"HTTP {response.status_code}"
        if response.status_code >= 500 and code == "gateway_error":
            code = UNREACHABLE
        raise GatewayError(response.status_code, code, message)
