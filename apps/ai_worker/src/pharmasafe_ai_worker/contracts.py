"""Data passed between the case workflow, the pipeline workflow and its activities.

**No personal data in workflow history.** Temporal stores every activity's input and
output. Everything here is therefore either redacted (report text with tokens, model
output quoting that text) or free of personal data (ids, counts, flags). The original
text and the token mapping stay in the database, and only the final activity, which
writes to the case, reads them back.
"""

from dataclasses import dataclass, field
from typing import Any

TASK_QUEUE = "ai-pipeline"
PIPELINE_WORKFLOW = "CasePipeline"


def pipeline_workflow_id(case_id: str, version: int) -> str:
    return f"ai-{case_id}-v{version}"


@dataclass
class PipelineInput:
    tenant_id: str
    case_id: str
    version: int


@dataclass
class Prepared:
    """The redacted report text; ``None`` text means the case has no readable source."""

    bundle_version: str
    document_id: str | None
    redacted_text: str | None


@dataclass
class StepInput:
    tenant_id: str
    case_id: str
    version: int
    bundle_version: str
    variables: dict[str, str]


@dataclass
class PlanInput:
    tenant_id: str
    case_id: str
    version: int
    prepared: Prepared
    extraction: dict[str, Any]


@dataclass
class Plan:
    """What the later steps need, derived from the extraction (redacted)."""

    reactions: list[dict[str, str]] = field(default_factory=list)  # {id, term}
    coding_items: list[dict[str, Any]] = field(default_factory=list)
    gaps: list[dict[str, str]] = field(default_factory=list)  # {path, description}


@dataclass
class Outputs:
    extraction: dict[str, Any] | None = None
    seriousness: dict[str, Any] | None = None
    coding: dict[str, Any] | None = None
    follow_up: dict[str, Any] | None = None


@dataclass
class FinalizeInput:
    tenant_id: str
    case_id: str
    version: int
    bundle_version: str
    outputs: Outputs
    ai_available: bool
    # Why the AI was not available, when it was not (no personal data).
    unavailable_reason: str | None = None


@dataclass
class PipelineSummary:
    case_id: str
    version: int
    bundle_version: str
    status: str
    automation_level: str
    flags: list[str]
    applied_changes: int


@dataclass
class NarrativeInput:
    tenant_id: str
    case_id: str
    version: int
