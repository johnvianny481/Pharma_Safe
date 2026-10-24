"""ASGI entrypoint: ``uvicorn pharmasafe_ai_gateway.main:app``."""

from fastapi import FastAPI

from pharmasafe_platform.logging import configure_logging
from pharmasafe_platform.settings import get_settings

SERVICE_NAME = "ai-gateway"


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(SERVICE_NAME, settings.log_level)
    app = FastAPI(title="PharmaSafe AI Gateway", version="0.1.0")

    @app.get("/healthz", tags=["ops"])
    def healthz() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    return app


app = create_app()
