"""Request-scoped context shared by logging and tracing.

The API sets these once the caller's token is verified. Logging and the span processor
read them, so every log line and span carries ``tenant_id`` (and ``case_id`` once a
request is about a case) without each call site passing them around.
"""

from contextvars import ContextVar

current_tenant_id: ContextVar[str | None] = ContextVar("current_tenant_id", default=None)
current_case_id: ContextVar[str | None] = ContextVar("current_case_id", default=None)
