from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from pharmasafe_api.main import create_app
from pharmasafe_platform.auth import TokenVerifier
from pharmasafe_platform.db import Database
from pharmasafe_platform.telemetry import ContextSpanProcessor


@pytest.fixture
def spans() -> InMemorySpanExporter:
    return InMemorySpanExporter()


@pytest.fixture
def client(
    db: Database, verifier: TokenVerifier, spans: InMemorySpanExporter
) -> Iterator[TestClient]:
    provider = TracerProvider()
    provider.add_span_processor(ContextSpanProcessor())
    provider.add_span_processor(SimpleSpanProcessor(spans))
    app = create_app(verifier=verifier, database=db, tracer_provider=provider)
    with TestClient(app) as test_client:
        yield test_client
