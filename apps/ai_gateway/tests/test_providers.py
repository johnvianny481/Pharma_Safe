"""Provider adapters: request shape, pinned-model checks and error classification."""

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from pharmasafe_ai_gateway.providers import (
    AzureOpenAI,
    OpenAI,
    ProviderError,
    ProviderRequest,
)
from pharmasafe_ai_gateway.schemas import json_schema
from pharmasafe_platform.aiconfig import ModelTarget, Step

OPENAI = ModelTarget(provider="openai", model="gpt-4o-2024-08-06", usd_per_1k_input=0.0025,
                     usd_per_1k_output=0.01)  # fmt: skip
AZURE = ModelTarget(provider="azure_openai", model="gpt-4o-2024-08-06", deployment="pv-gpt4o",
                    api_version="2024-10-21", usd_per_1k_input=0.0025, usd_per_1k_output=0.01)  # fmt: skip


def _request(target: ModelTarget) -> ProviderRequest:
    return ProviderRequest(
        step=Step.FOLLOW_UP,
        target=target,
        system="system text",
        user="user text",
        schema=json_schema(Step.FOLLOW_UP),
        max_tokens=800,
        timeout_seconds=5,
        temperature=0.0,
        variables={"gaps": "[]"},
    )


def _reply(model: str = "gpt-4o-2024-08-06", **overrides: Any) -> dict[str, Any]:
    message = {"role": "assistant", "content": json.dumps({"questions": []})}
    return {
        "model": model,
        "choices": [{"message": message | overrides.pop("message", {}),
                     "finish_reason": overrides.pop("finish_reason", "stop")}],
        "usage": {"prompt_tokens": 120, "completion_tokens": 8},
    } | overrides  # fmt: skip


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_openai_request_has_strict_schema_pinned_model_and_no_tools() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply())

    response = OpenAI(_client(handler), "https://api.openai.test/v1", "sk-test").complete(
        _request(OPENAI)
    )
    (request,) = seen
    body = json.loads(request.content)
    assert str(request.url) == "https://api.openai.test/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    assert body["model"] == "gpt-4o-2024-08-06"
    assert body["max_tokens"] == 800
    assert body["response_format"]["type"] == "json_schema"
    assert body["response_format"]["json_schema"]["strict"] is True
    assert "tools" not in body
    assert "functions" not in body
    assert "tool_choice" not in body
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert (response.input_tokens, response.output_tokens) == (120, 8)


def test_azure_uses_the_deployment_and_pinned_api_version() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=_reply(model="gpt-4o"))

    AzureOpenAI(_client(handler), "https://pv.openai.azure.test/", "key").complete(_request(AZURE))
    (request,) = seen
    assert str(request.url) == (
        "https://pv.openai.azure.test/openai/deployments/pv-gpt4o/chat/completions"
        "?api-version=2024-10-21"
    )
    assert request.headers["api-key"] == "key"
    assert "model" not in json.loads(request.content)


@pytest.mark.parametrize(
    ("target", "served"),
    [(OPENAI, "gpt-4o"), (OPENAI, "gpt-4o-2024-11-20"), (AZURE, "gpt-4o-mini")],
)
def test_a_different_model_than_the_pinned_one_is_an_error(
    target: ModelTarget, served: str
) -> None:
    adapter = (
        OpenAI(_client(lambda _r: httpx.Response(200, json=_reply(model=served))), "https://x", "k")
        if target is OPENAI
        else AzureOpenAI(
            _client(lambda _r: httpx.Response(200, json=_reply(model=served))), "https://x", "k"
        )
    )
    with pytest.raises(ProviderError, match="pinned") as error:
        adapter.complete(_request(target))
    assert not error.value.retryable


@pytest.mark.parametrize(
    ("status", "retryable"), [(429, True), (500, True), (503, True), (400, False), (401, False)]
)
def test_http_errors_are_classified(status: int, retryable: bool) -> None:
    adapter = OpenAI(_client(lambda _r: httpx.Response(status)), "https://x", "k")
    with pytest.raises(ProviderError) as error:
        adapter.complete(_request(OPENAI))
    assert error.value.retryable is retryable


def test_timeouts_and_connection_failures_are_retryable() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def refused(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    for handler in (timeout, refused):
        with pytest.raises(ProviderError) as error:
            OpenAI(_client(handler), "https://x", "k").complete(_request(OPENAI))
        assert error.value.retryable


@pytest.mark.parametrize(
    ("reply", "message", "retryable"),
    [
        (_reply(message={"refusal": "I cannot help"}), "refused", False),
        (_reply(finish_reason="length"), "truncated", False),
        (_reply(message={"content": None}), "no content", True),
        ({"unexpected": True}, "malformed", True),
    ],
)
def test_unusable_answers(reply: dict[str, Any], message: str, retryable: bool) -> None:
    adapter = OpenAI(_client(lambda _r: httpx.Response(200, json=reply)), "https://x", "k")
    with pytest.raises(ProviderError, match=message) as error:
        adapter.complete(_request(OPENAI))
    assert error.value.retryable is retryable
