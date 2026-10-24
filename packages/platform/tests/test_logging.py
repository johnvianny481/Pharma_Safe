import json
import logging

from pharmasafe_platform.logging import JsonFormatter


def _record(**extra: object) -> logging.LogRecord:
    record = logging.LogRecord("test", logging.INFO, __file__, 1, "hello %s", ("world",), None)
    for key, value in extra.items():
        setattr(record, key, value)
    return record


def test_formats_as_single_json_object() -> None:
    payload = json.loads(JsonFormatter("api").format(_record()))
    assert payload["message"] == "hello world"
    assert payload["service"] == "api"
    assert payload["level"] == "INFO"


def test_includes_case_and_tenant_context() -> None:
    payload = json.loads(JsonFormatter("api").format(_record(tenant_id="t1", case_id="c1")))
    assert payload["tenant_id"] == "t1"
    assert payload["case_id"] == "c1"


def test_drops_unlisted_extra_keys() -> None:
    payload = json.loads(JsonFormatter("api").format(_record(patient_name="Jane")))
    assert "patient_name" not in payload
