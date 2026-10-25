from datetime import UTC, datetime
from uuid import uuid4

from pharmasafe_platform.outbox import OutboxMessage
from pharmasafe_workflow_worker.publisher import to_signal


def _message(topic: str, event_type: str, **payload: object) -> OutboxMessage:
    return OutboxMessage(uuid4(), uuid4(), topic, event_type, "agg", payload, datetime.now(UTC))


def test_case_events_become_signals_to_the_case_workflow() -> None:
    case_id = str(uuid4())
    task_id = str(uuid4())
    message = _message(
        "cases", "task.completed", case_id=case_id, task_id=task_id, kind="data_entry", version=2
    )
    signal = to_signal(message)
    assert signal is not None
    wf_id, start, event = signal
    assert wf_id == f"case-{case_id}"
    assert (start.tenant_id, start.case_id) == (str(message.tenant_id), case_id)
    assert (event.event_id, event.type, event.version, event.task_id, event.kind) == (
        str(message.id),
        "task.completed",
        2,
        task_id,
        "data_entry",
    )


def test_other_topics_are_not_for_the_workflow() -> None:
    assert to_signal(_message("intake", "intake.received", submission_id="s")) is None
    assert to_signal(_message("notifications", "case.deadline_escalated", case_id="c")) is None
