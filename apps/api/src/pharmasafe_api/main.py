"""ASGI entrypoint: ``uvicorn pharmasafe_api.main:app``."""

from datetime import timedelta

from fastapi import FastAPI
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider

from pharmasafe_api.audit.router import router as audit_router
from pharmasafe_api.cases.router import router as cases_router
from pharmasafe_api.identity.router import router as identity_router
from pharmasafe_api.intake.extraction import OcrEngine, TesseractOcr
from pharmasafe_api.intake.router import router as intake_router
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.auth import TokenVerifier, jwks_key_resolver
from pharmasafe_platform.blobstore import AzureBlobStore, BlobStore
from pharmasafe_platform.db import Database
from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.settings import Settings, get_settings
from pharmasafe_platform.signatures import SignatureService
from pharmasafe_platform.telemetry import configure_tracing

SERVICE_NAME = "api"


def create_app(
    settings: Settings | None = None,
    *,
    verifier: TokenVerifier | None = None,
    database: Database | None = None,
    blobs: BlobStore | None = None,
    ocr: OcrEngine | None = None,
    tracer_provider: TracerProvider | None = None,
) -> FastAPI:
    """Build the app. Tests inject a verifier with local keys and a test database."""
    settings = settings or get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    app = FastAPI(title="PharmaSafe API", version="0.1.0")

    app.state.settings = settings
    app.state.verifier = verifier or TokenVerifier(
        issuer=settings.oidc_issuer,
        audience=settings.oidc_audience,
        key_resolver=jwks_key_resolver(settings.jwks_url),
    )
    # create_engine does not connect, so building the app needs no running database.
    app.state.database = database or Database(settings.database_url)
    app.state.signatures = SignatureService(
        max_auth_age=timedelta(seconds=settings.signature_max_auth_age_seconds)
    )
    app.state.blobs = blobs or AzureBlobStore(
        settings.blob_endpoint,
        settings.blob_account,
        settings.blob_key.get_secret_value(),
        settings.blob_container,
    )
    # None when Tesseract is not installed: images are then held for manual entry.
    app.state.ocr = ocr or TesseractOcr.find(settings.tesseract_cmd)
    # Example rule sets for now; per-tenant configuration arrives with the admin screens.
    app.state.clock_rules = seed_registry()

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    app.include_router(identity_router)
    app.include_router(audit_router)
    app.include_router(cases_router)
    app.include_router(intake_router)

    FastAPIInstrumentor.instrument_app(
        app,
        tracer_provider=tracer_provider or configure_tracing(SERVICE_NAME),
        excluded_urls="healthz",
    )
    return app


app = create_app()
