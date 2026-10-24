"""Structured JSON logging.

Every log line is one JSON object so it can be shipped to Azure Monitor / Grafana and
joined to traces. Callers attach context (``case_id``, ``tenant_id``) through ``extra``.
Personal data must never be passed to a logger; redaction happens upstream of logging.
"""

import json
import logging
import sys
from datetime import UTC, datetime
from typing import Any

from opentelemetry import trace

from pharmasafe_platform.context import current_case_id, current_tenant_id

# Context keys copied from ``extra`` onto the JSON record when present. Anything else in
# ``extra`` is dropped, so an accidental ``extra={"patient_name": ...}`` never reaches logs.
CONTEXT_KEYS = ("tenant_id", "case_id", "case_version", "trace_id", "span_id", "temporal_address")


class JsonFormatter(logging.Formatter):
    def __init__(self, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "service": self._service_name,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in CONTEXT_KEYS:
            value = getattr(record, key, None)
            if value is not None:
                payload[key] = value
        # Fall back to the request context and the active span.
        payload.setdefault("tenant_id", current_tenant_id.get())
        payload.setdefault("case_id", current_case_id.get())
        span_context = trace.get_current_span().get_span_context()
        if span_context.is_valid:
            payload.setdefault("trace_id", format(span_context.trace_id, "032x"))
            payload.setdefault("span_id", format(span_context.span_id, "016x"))
        payload = {k: v for k, v in payload.items() if v is not None}
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def configure_logging(service_name: str, level: str = "INFO") -> None:
    """Route the root logger to stdout as JSON. Safe to call more than once."""
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter(service_name))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
