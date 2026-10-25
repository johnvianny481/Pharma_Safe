"""ASGI entrypoint: ``uvicorn pharmasafe_ai_gateway.main:app``.

``POST /v1/steps/{step}`` runs one pipeline step's model call. Callers are internal
services: they present the gateway token and name the tenant in ``X-Tenant-Id``. In
Azure the services also authenticate each other with mutual TLS (Phase 10), and the
gateway is reachable only on the private network.

Providers are registered from settings: Azure OpenAI and OpenAI when their keys are set,
and MockLLM only outside validation and production, so a production bundle can never be
served by the mock.
"""

import hmac
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

import httpx
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from pharmasafe_ai_gateway.langfuse import CallLog, LangfuseLog, NoCallLog
from pharmasafe_ai_gateway.ledger import Ledger
from pharmasafe_ai_gateway.mock import MockLLM
from pharmasafe_ai_gateway.providers import AzureOpenAI, OpenAI, Provider
from pharmasafe_ai_gateway.service import GatewayError, GatewayService, StepCall
from pharmasafe_platform.aiconfig import BundleStore, ProviderName, Step
from pharmasafe_platform.db import Database
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.settings import Environment, Settings, get_settings

SERVICE_NAME = "ai-gateway"


class StepRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=100)
    bundle_version: str = Field(min_length=1, max_length=64)
    variables: dict[str, str]


class StepResponse(BaseModel):
    output: dict[str, Any]
    provider: str
    model: str
    prompt_version: str
    fallback_used: bool
    call_id: str


def build_service(settings: Settings) -> GatewayService:
    http = httpx.Client()
    providers: dict[str, Provider] = {}
    if settings.azure_openai_endpoint and settings.azure_openai_key.get_secret_value():
        providers[ProviderName.AZURE_OPENAI.value] = AzureOpenAI(
            http, settings.azure_openai_endpoint, settings.azure_openai_key.get_secret_value()
        )
    if settings.openai_api_key.get_secret_value():
        providers[ProviderName.OPENAI.value] = OpenAI(
            http, settings.openai_base_url, settings.openai_api_key.get_secret_value()
        )
    if settings.environment in (Environment.DEV, Environment.TEST):
        providers[ProviderName.MOCK.value] = MockLLM()
    call_log: CallLog = NoCallLog()
    if settings.langfuse_public_key and settings.langfuse_secret_key.get_secret_value():
        call_log = LangfuseLog(
            http,
            settings.langfuse_host,
            settings.langfuse_public_key,
            settings.langfuse_secret_key.get_secret_value(),
        )
    return GatewayService(
        BundleStore(Path(settings.ai_bundle_dir)),
        providers,
        Ledger(Database(settings.database_url), settings.ai_default_monthly_budget_usd),
        call_log,
    )


def create_app(service: GatewayService | None = None, token: str | None = None) -> FastAPI:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    app = FastAPI(title="PharmaSafe AI Gateway", version="0.1.0")
    expected_token = token if token is not None else settings.ai_gateway_token.get_secret_value()
    lazy: dict[str, GatewayService] = {}

    def gateway() -> GatewayService:
        if service is not None:
            return service
        if "service" not in lazy:
            lazy["service"] = build_service(settings)
        return lazy["service"]

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.post("/v1/steps/{step}", response_model=StepResponse, tags=["ai"])
    def run_step(
        step: Step,
        body: StepRequest,
        authorization: Annotated[str, Header()] = "",
        x_tenant_id: Annotated[UUID | None, Header()] = None,
    ) -> Any:
        presented = authorization.removeprefix("Bearer ")
        if not expected_token or not hmac.compare_digest(presented, expected_token):
            raise HTTPException(401, "invalid gateway token")
        if x_tenant_id is None:
            raise HTTPException(400, "X-Tenant-Id is required")
        call = StepCall(x_tenant_id, body.case_id, step, body.bundle_version, body.variables)
        try:
            result = gateway().run_step(call)
        except GatewayError as exc:
            return JSONResponse(
                status_code=exc.status, content={"code": exc.code, "message": str(exc)}
            )
        return StepResponse(**result.__dict__)

    return app


app = create_app()
