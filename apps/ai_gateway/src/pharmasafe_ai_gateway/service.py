"""The gateway's core: one model call for one pipeline step, under every control.

In order, for each call:

1. **Bundle.** The step's prompt and route come from the signed bundle version the
   caller names. An unknown or untrusted bundle is refused.
2. **Inputs.** The step must receive exactly its declared variables, within the route's
   size limit.
3. **Redaction check.** The redaction rules run again on every variable. Detectable
   personal data means the caller did not redact, and the call is refused (the reply
   names the kinds found, never the values).
4. **Budget.** The month's spend plus this call's worst case must fit the tenant's limit.
5. **Routing.** The primary target is tried up to ``max_attempts`` times with exponential
   backoff and jitter on retryable errors (timeouts, 429, 5xx, malformed or
   schema-invalid output). Then the fallback target the same way. If both fail, the
   caller gets ``providers_unavailable`` and processes the case in L0 manual mode.
6. **Output.** The answer must validate against the step's schema.
7. **Records.** Every outcome is written to the ``ai_calls`` ledger and logged
   (redacted) to Langfuse.
"""

import logging
import random
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from pydantic import ValidationError

from pharmasafe_ai_gateway.langfuse import CallLog, Generation
from pharmasafe_ai_gateway.ledger import CallRecord
from pharmasafe_ai_gateway.prompts import Rendered, render
from pharmasafe_ai_gateway.providers import Provider, ProviderError, ProviderRequest
from pharmasafe_ai_gateway.schemas import OUTPUT_MODELS, STEP_VARIABLES, json_schema
from pharmasafe_domain.ai.redaction import find_identifiers
from pharmasafe_platform.aiconfig import BundleError, BundleStore, ModelTarget, Step

log = logging.getLogger(__name__)


class GatewayError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass(frozen=True)
class StepCall:
    tenant_id: UUID
    case_id: str
    step: Step
    bundle_version: str
    variables: dict[str, str]


@dataclass(frozen=True)
class StepResult:
    output: dict[str, Any]
    provider: str
    model: str
    prompt_version: str
    fallback_used: bool
    call_id: str


class LedgerLike(Protocol):
    def remaining(self, tenant_id: UUID, now: datetime) -> Decimal: ...

    def record(self, call: CallRecord, now: datetime) -> UUID: ...


def cost(target: ModelTarget, input_tokens: int, output_tokens: int) -> Decimal:
    usd = input_tokens / 1000 * target.usd_per_1k_input
    usd += output_tokens / 1000 * target.usd_per_1k_output
    return Decimal(str(round(usd, 6)))


class GatewayService:
    def __init__(
        self,
        bundles: BundleStore,
        providers: Mapping[str, Provider],
        ledger: LedgerLike,
        call_log: CallLog,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], None] = time.sleep,
        backoff_base: float = 0.5,
        backoff_max: float = 8.0,
    ) -> None:
        self._bundles = bundles
        self._providers = providers
        self._ledger = ledger
        self._log = call_log
        self._clock = clock
        self._sleep = sleep
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max

    def run_step(self, call: StepCall) -> StepResult:
        try:
            bundle = self._bundles.get(call.bundle_version)
        except BundleError as exc:
            raise GatewayError(404, "unknown_bundle", str(exc)) from exc
        route, prompt = bundle.routes[call.step], bundle.prompts[call.step]
        expected = STEP_VARIABLES[call.step]
        if set(call.variables) != expected:
            raise GatewayError(400, "bad_variables", f"{call.step} takes {sorted(expected)}")
        size = sum(len(v) for v in call.variables.values())
        if size > route.max_input_chars:
            raise GatewayError(413, "input_too_large", f"{size} characters over the route limit")
        found = sorted({f.kind.value for v in call.variables.values() for f in find_identifiers(v)})
        if found:
            self._record(call, prompt.version, None, 0, False, "refused_unredacted", 0, 0, 0)
            raise GatewayError(
                422, "unredacted_personal_data", f"personal data found: {', '.join(found)}"
            )

        rendered = render(prompt, call.variables)
        targets = [route.primary] + ([route.fallback] if route.fallback else [])
        input_estimate = (len(rendered.system) + len(rendered.user)) // 3
        worst = max(cost(t, input_estimate, route.max_tokens) for t in targets)
        if self._ledger.remaining(call.tenant_id, self._clock()) < worst:
            self._record(call, prompt.version, None, 0, False, "refused_budget", 0, 0, 0)
            raise GatewayError(429, "budget_exceeded", "the tenant's monthly AI budget is spent")

        schema = json_schema(call.step)
        attempts = 0
        started = time.monotonic()
        for index, target in enumerate(targets):
            provider = self._providers.get(target.provider.value)
            if provider is None:
                log.warning("provider not configured", extra={"provider": target.provider.value})
                continue
            request = ProviderRequest(
                step=call.step,
                target=target,
                system=rendered.system,
                user=rendered.user,
                schema=schema,
                max_tokens=route.max_tokens,
                timeout_seconds=route.timeout_seconds,
                temperature=route.temperature,
                variables=call.variables,
            )
            for attempt in range(route.max_attempts):
                attempts += 1
                begun = self._clock()
                try:
                    response = provider.complete(request)
                    output = OUTPUT_MODELS[call.step].model_validate_json(response.content)
                except ProviderError as exc:
                    self._trace(call, prompt.version, target, rendered, None, 0, 0, begun, "error")
                    log.warning(
                        "model call failed",
                        extra={"step": call.step.value, "provider": target.provider.value,
                               "attempt": attempt + 1, "reason": str(exc)},
                    )  # fmt: skip
                    if not exc.retryable:
                        break
                except ValidationError:
                    self._trace(
                        call, prompt.version, target, rendered, None, 0, 0, begun, "invalid_output"
                    )
                else:
                    spent = cost(target, response.input_tokens, response.output_tokens)
                    latency = int((time.monotonic() - started) * 1000)
                    fallback = index > 0
                    call_id = self._record(
                        call, prompt.version, target, attempts, fallback,
                        "fallback_ok" if fallback else "ok",
                        response.input_tokens, response.output_tokens, latency, spent,
                    )  # fmt: skip
                    self._trace(
                        call, prompt.version, target, rendered, response.content,
                        response.input_tokens, response.output_tokens, begun, "ok", spent,
                    )  # fmt: skip
                    return StepResult(
                        output=output.model_dump(mode="json"),
                        provider=target.provider.value,
                        model=response.model,
                        prompt_version=prompt.version,
                        fallback_used=fallback,
                        call_id=str(call_id),
                    )
                if attempt + 1 < route.max_attempts:
                    self._sleep(self._backoff(attempt))
        latency = int((time.monotonic() - started) * 1000)
        self._record(call, prompt.version, None, attempts, False, "failed", 0, 0, latency)
        raise GatewayError(503, "providers_unavailable", "every configured provider failed")

    def _backoff(self, attempt: int) -> float:
        delay: float = min(self._backoff_base * 2.0**attempt, self._backoff_max)
        # Jitter for backoff, not cryptography: a non-secure generator is right here.
        jitter = 0.5 + random.random() / 2  # noqa: S311  # nosec B311
        return delay * jitter

    def _record(
        self,
        call: StepCall,
        prompt_version: str,
        target: ModelTarget | None,
        attempts: int,
        fallback: bool,
        outcome: str,
        input_tokens: int,
        output_tokens: int,
        latency_ms: int,
        spent: Decimal = Decimal(0),
    ) -> UUID:
        return self._ledger.record(
            CallRecord(
                tenant_id=call.tenant_id,
                case_id=call.case_id,
                step=call.step.value,
                bundle_version=call.bundle_version,
                prompt_version=prompt_version,
                provider=target.provider.value if target else None,
                model=target.model if target else None,
                attempts=attempts,
                fallback_used=fallback,
                outcome=outcome,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=spent,
                latency_ms=latency_ms,
            ),
            self._clock(),
        )

    def _trace(
        self,
        call: StepCall,
        prompt_version: str,
        target: ModelTarget,
        rendered: Rendered,
        output: str | None,
        input_tokens: int,
        output_tokens: int,
        started: datetime,
        outcome: str,
        spent: Decimal = Decimal(0),
    ) -> None:
        self._log.record(
            Generation(
                tenant_id=str(call.tenant_id),
                case_id=call.case_id,
                step=call.step.value,
                bundle_version=call.bundle_version,
                prompt_version=prompt_version,
                provider=target.provider.value,
                model=target.model,
                system=rendered.system,
                user=rendered.user,
                output=output,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cost_usd=float(spent),
                started_at=started,
                ended_at=self._clock(),
                outcome=outcome,
            )
        )
