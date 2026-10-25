"""The gateway over HTTP: authentication, error mapping, and the worker's client."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from pharmasafe_ai_gateway.client import UNREACHABLE, HttpGateway
from pharmasafe_ai_gateway.langfuse import Generation, LangfuseLog, events
from pharmasafe_ai_gateway.main import create_app
from pharmasafe_ai_gateway.service import GatewayError, GatewayService, StepCall
from pharmasafe_platform.aiconfig import BundleStore, Step

TOKEN = "gateway-test-token"
REPORT = "Patient JD developed a rash after starting Acmezol 20 mg."


@pytest.fixture
def api(
    make_bundles: Callable[..., tuple[BundleStore, str]],
    make_gateway: Callable[..., GatewayService],
) -> tuple[TestClient, str]:
    bundles, version = make_bundles("gw-api-1")
    return TestClient(create_app(make_gateway(bundles), token=TOKEN)), version


def _body(version: str, **variables: str) -> dict[str, Any]:
    return {
        "case_id": "c-1",
        "bundle_version": version,
        "variables": variables or {"report": REPORT},
    }


def test_internal_callers_need_the_token_and_a_tenant(api: tuple[TestClient, str]) -> None:
    client, version = api
    no_token = client.post("/v1/steps/extraction", json=_body(version))
    assert no_token.status_code == 401
    wrong = client.post(
        "/v1/steps/extraction", json=_body(version), headers={"Authorization": "Bearer nope"}
    )
    assert wrong.status_code == 401
    no_tenant = client.post(
        "/v1/steps/extraction", json=_body(version), headers={"Authorization": f"Bearer {TOKEN}"}
    )
    assert no_tenant.status_code == 400


def test_a_gateway_without_a_configured_token_refuses_everyone(
    make_bundles: Callable[..., tuple[BundleStore, str]],
    make_gateway: Callable[..., GatewayService],
) -> None:
    bundles, version = make_bundles("gw-api-2")
    client = TestClient(create_app(make_gateway(bundles), token=""))
    response = client.post("/v1/steps/extraction", json=_body(version),
                           headers={"Authorization": "Bearer "})  # fmt: skip
    assert response.status_code == 401


def test_the_worker_client_runs_steps_over_http(api: tuple[TestClient, str], tenant_a: Any) -> None:
    client, version = api
    gateway = HttpGateway(client, "http://testserver", TOKEN)
    result = gateway.run_step(
        StepCall(tenant_a, "c-1", Step.EXTRACTION, version, {"report": REPORT})
    )
    assert result.output["reactions"][0]["reported_term"]["value"] == "rash"
    assert result.prompt_version == "extraction-v1"


def test_gateway_errors_keep_their_code_over_http(
    api: tuple[TestClient, str], tenant_a: Any
) -> None:
    client, version = api
    gateway = HttpGateway(client, "http://testserver", TOKEN)
    with pytest.raises(GatewayError) as error:
        gateway.run_step(
            StepCall(tenant_a, "c-1", Step.EXTRACTION, version, {"report": "mail me: a@b.example"})
        )
    assert (error.value.status, error.value.code) == (422, "unredacted_personal_data")


def test_an_unreachable_gateway_is_reported_as_such() -> None:
    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    gateway = HttpGateway(httpx.Client(transport=httpx.MockTransport(down)), "http://gw", TOKEN)
    with pytest.raises(GatewayError) as error:
        gateway.run_step(StepCall(uuid4(), "c", Step.EXTRACTION, "v", {"report": "x"}))
    assert error.value.code == UNREACHABLE
    bare_500 = HttpGateway(
        httpx.Client(transport=httpx.MockTransport(lambda _r: httpx.Response(502))),
        "http://gw",
        "t",
    )
    with pytest.raises(GatewayError) as error:
        bare_500.run_step(StepCall(uuid4(), "c", Step.EXTRACTION, "v", {"report": "x"}))
    assert error.value.code == UNREACHABLE


# ---------------------------------------------------------------- Langfuse


def _generation() -> Generation:
    now = datetime.now(UTC)
    return Generation(
        tenant_id="t-1", case_id="c-1", step="extraction", bundle_version="b1",
        prompt_version="extraction-v1", provider="mock", model="mock-pv-v1",
        system="sys", user="Patient [NAME_1] ...", output="{}", input_tokens=10,
        output_tokens=2, cost_usd=0.001, started_at=now, ended_at=now, outcome="ok",
    )  # fmt: skip


def test_langfuse_gets_one_trace_per_case_and_one_generation_per_call() -> None:
    trace, generation = events(_generation())
    assert trace["type"] == "trace-create"
    assert trace["body"]["id"] == "case-c-1"
    assert generation["type"] == "generation-create"
    body = generation["body"]
    assert body["traceId"] == "case-c-1"
    assert body["model"] == "mock-pv-v1"
    assert body["usage"] == {"input": 10, "output": 2, "unit": "TOKENS"}
    assert body["metadata"]["prompt_version"] == "extraction-v1"
    assert body["metadata"]["bundle_version"] == "b1"


def test_langfuse_is_posted_in_the_background_and_failures_are_swallowed() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(207, json={"successes": [], "errors": []})

    log = LangfuseLog(httpx.Client(transport=httpx.MockTransport(handler)),
                      "http://langfuse.test/", "pk", "sk")  # fmt: skip
    log.record(_generation())
    log.flush()
    (request,) = seen
    assert str(request.url) == "http://langfuse.test/api/public/ingestion"
    assert request.headers["authorization"].startswith("Basic ")

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    failing = LangfuseLog(httpx.Client(transport=httpx.MockTransport(down)), "http://x", "pk", "sk")
    failing.record(_generation())
    failing.flush()  # no exception reaches the caller
