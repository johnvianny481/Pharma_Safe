"""OpenTelemetry tracing setup.

:class:`ContextSpanProcessor` stamps ``tenant_id`` and ``case_id`` from the request context
onto every span when it starts, so traces can always be filtered by tenant and case.
Spans are exported over OTLP only when ``OTEL_EXPORTER_OTLP_ENDPOINT`` is set (standard
OpenTelemetry variable); otherwise tracing still runs but nothing leaves the process.
"""

import os

from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import ReadableSpan, Span, SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from pharmasafe_platform.context import current_case_id, current_tenant_id

TENANT_ATTRIBUTE = "tenant_id"
CASE_ATTRIBUTE = "case_id"


class ContextSpanProcessor(SpanProcessor):
    def on_start(self, span: Span, parent_context: Context | None = None) -> None:
        tenant_id = current_tenant_id.get()
        if tenant_id is not None:
            span.set_attribute(TENANT_ATTRIBUTE, tenant_id)
        case_id = current_case_id.get()
        if case_id is not None:
            span.set_attribute(CASE_ATTRIBUTE, case_id)

    def on_end(self, span: ReadableSpan) -> None:
        return None


def build_tracer_provider(service_name: str) -> TracerProvider:
    provider = TracerProvider(resource=Resource.create({SERVICE_NAME: service_name}))
    provider.add_span_processor(ContextSpanProcessor())
    if os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    return provider


_global_provider: TracerProvider | None = None


def configure_tracing(service_name: str) -> TracerProvider:
    """Install the process-wide tracer provider once and return it."""
    global _global_provider
    if _global_provider is None:
        _global_provider = build_tracer_provider(service_name)
        trace.set_tracer_provider(_global_provider)
    return _global_provider


def tag_current_span(tenant_id: str | None = None, case_id: str | None = None) -> None:
    """Tag a span that started before the context was known (e.g. the HTTP server span)."""
    span = trace.get_current_span()
    if tenant_id is not None:
        span.set_attribute(TENANT_ATTRIBUTE, tenant_id)
    if case_id is not None:
        span.set_attribute(CASE_ATTRIBUTE, case_id)
