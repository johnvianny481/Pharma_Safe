"""Provider adapters: Azure OpenAI and OpenAI chat completions, over httpx.

Both use the chat completions API with strict structured output. The request never
contains tools or functions. The adapters do no retrying: retries, backoff and fallback
live in one place, :mod:`pharmasafe_ai_gateway.service`.

**Pinned versions are checked on the way back too.** The response names the model that
served the call, and a different model than the one pinned is an error, so a silent
upgrade on the provider side is caught rather than absorbed.
"""

from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from pharmasafe_platform.aiconfig import ModelTarget, Step

RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 504})


@dataclass(frozen=True)
class ProviderRequest:
    step: Step
    target: ModelTarget
    system: str
    user: str
    schema: dict[str, Any]
    max_tokens: int
    timeout_seconds: float
    temperature: float
    # The unrendered variables, for MockLLM only; real providers see only the messages.
    variables: dict[str, str]


@dataclass(frozen=True)
class ProviderResponse:
    content: str
    model: str
    input_tokens: int
    output_tokens: int


class ProviderError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class Provider(Protocol):
    def complete(self, request: ProviderRequest) -> ProviderResponse: ...


class _ChatCompletions:
    """Shared request and response handling of the chat completions API."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client

    def complete(self, request: ProviderRequest) -> ProviderResponse:
        body: dict[str, Any] = {
            "messages": [
                {"role": "system", "content": request.system},
                {"role": "user", "content": request.user},
            ],
            "max_tokens": request.max_tokens,
            "temperature": request.temperature,
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": f"{request.step.value}_output",
                    "schema": request.schema,
                    "strict": True,
                },
            },
        }
        url, headers = self._endpoint(request.target)
        if "model" in self._body_fields():
            body["model"] = request.target.model
        try:
            response = self._client.post(
                url, json=body, headers=headers, timeout=request.timeout_seconds
            )
        except httpx.TimeoutException as exc:
            raise ProviderError("provider timed out", retryable=True) from exc
        except httpx.TransportError as exc:
            raise ProviderError(
                f"provider unreachable: {type(exc).__name__}", retryable=True
            ) from exc
        if response.status_code != 200:
            raise ProviderError(
                f"provider returned HTTP {response.status_code}",
                retryable=response.status_code in RETRYABLE_STATUS,
            )
        return self._parse(response, request.target)

    def _parse(self, response: httpx.Response, target: ModelTarget) -> ProviderResponse:
        try:
            data = response.json()
            choice = data["choices"][0]
            message = choice["message"]
            usage = data.get("usage") or {}
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ProviderError("malformed provider response", retryable=True) from exc
        if message.get("refusal"):
            raise ProviderError("the model refused the request", retryable=False)
        if choice.get("finish_reason") == "length":
            raise ProviderError("output truncated at max_tokens", retryable=False)
        served = str(data.get("model", ""))
        if not self._accepts(served, target):
            raise ProviderError(
                f"provider served {served!r}, not the pinned {target.model!r}", retryable=False
            )
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("provider returned no content", retryable=True)
        return ProviderResponse(
            content=content,
            model=served,
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
        )

    def _endpoint(self, target: ModelTarget) -> tuple[str, dict[str, str]]:
        raise NotImplementedError

    def _body_fields(self) -> frozenset[str]:
        return frozenset()

    def _accepts(self, served: str, target: ModelTarget) -> bool:
        return served == target.model


class AzureOpenAI(_ChatCompletions):
    def __init__(self, client: httpx.Client, endpoint: str, api_key: str) -> None:
        super().__init__(client)
        self._endpoint_url = endpoint.rstrip("/")
        self._key = api_key

    def _endpoint(self, target: ModelTarget) -> tuple[str, dict[str, str]]:
        url = (
            f"{self._endpoint_url}/openai/deployments/{target.deployment}/chat/completions"
            f"?api-version={target.api_version}"
        )
        return url, {"api-key": self._key}

    def _accepts(self, served: str, target: ModelTarget) -> bool:
        # The deployment pins the model version on the Azure side, and some API versions
        # report only the base name ("gpt-4o"). Any other model is a different model.
        return served in (target.model, target.model.rsplit("-", 3)[0])


class OpenAI(_ChatCompletions):
    def __init__(self, client: httpx.Client, base_url: str, api_key: str) -> None:
        super().__init__(client)
        self._base = base_url.rstrip("/")
        self._key = api_key

    def _endpoint(self, target: ModelTarget) -> tuple[str, dict[str, str]]:
        return f"{self._base}/chat/completions", {"Authorization": f"Bearer {self._key}"}

    def _body_fields(self) -> frozenset[str]:
        return frozenset({"model"})
