"""Data passed between the case workflow, its activities and the event relay.

Plain dataclasses of strings and numbers only, so Temporal can serialise them and the
workflow sandbox can import this module without any I/O dependencies. Dates and times
travel as ISO 8601 strings.
"""

from dataclasses import dataclass, field

TASK_QUEUE = "case-workflow"
WORKFLOW_NAME = "CaseWorkflow"
SIGNAL = "case_event"


def workflow_id(case_id: str) -> str:
    return f"case-{case_id}"


@dataclass
class CaseWorkflowInput:
    tenant_id: str
    case_id: str
    # Escalate this many days before a regulatory due date (warning, then escalation).
    warn_days: int = 5
    escalate_days: int = 2
    # Set when the workflow continues as new after a follow-up, to skip intake stages.
    resume: bool = False


@dataclass
class CaseEvent:
    """An outbox event delivered as a signal. ``event_id`` deduplicates redeliveries."""

    event_id: str
    type: str
    version: int | None = None
    task_id: str | None = None
    kind: str | None = None


@dataclass
class WorkflowState:
    """Answer to the workflow's ``state`` query."""

    workflow_version: int
    stage: str
    version: int


@dataclass
class CaseRef:
    tenant_id: str
    case_id: str


@dataclass
class DeadlineInfo:
    regulator: str
    rule_id: str
    case_version: int
    due_date: str


@dataclass
class TriageResult:
    version: int
    approved: bool
    valid: bool
    # Review task kinds in order, e.g. ["data_entry", "quality_review", "approval"].
    steps: list[str] = field(default_factory=list)
    deadlines: list[DeadlineInfo] = field(default_factory=list)


@dataclass
class StageRequest:
    tenant_id: str
    case_id: str
    stage: str
    at: str


@dataclass
class OpenTaskRequest:
    tenant_id: str
    case_id: str
    version: int
    kind: str
    task_id: str
    at: str


@dataclass
class EscalationRequest:
    tenant_id: str
    case_id: str
    deadline: DeadlineInfo
    level: str
    at: str
