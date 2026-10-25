"""Output schemas for each pipeline step: what a model is allowed to answer.

Each schema is sent to the provider as a strict JSON schema (structured output), and
every answer is validated against it again here. Extra keys, missing keys and wrong
types are refused. Validation here is structural only: whether a quote is really in the
report, or a code is one of the candidates, is checked by the AI worker, which knows
the inputs.

The models follow the provider's strict-mode rules: every property is required (absent
values are ``null``), no defaults, no additional properties. Enumerated values travel as
strings and are checked against the case model by the worker, so a schema change is
never needed to add a code.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from pharmasafe_platform.aiconfig import Step


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class TextValue(_Strict):
    value: str
    quote: str
    confidence: float


class NumberValue(_Strict):
    value: float
    unit: str | None
    quote: str
    confidence: float


class PatientOut(_Strict):
    initials: TextValue | None
    sex: TextValue | None
    age: NumberValue | None
    weight_kg: NumberValue | None


class ReporterOut(_Strict):
    qualification: TextValue | None
    given_name: TextValue | None
    family_name: TextValue | None
    country: TextValue | None


class DrugOut(_Strict):
    product_name: TextValue
    role: TextValue | None
    dose: NumberValue | None
    route: TextValue | None
    start_date: TextValue | None
    indication: TextValue | None


class ReactionOut(_Strict):
    reported_term: TextValue
    onset_date: TextValue | None
    outcome: TextValue | None


class ExtractionOut(_Strict):
    patient: PatientOut
    reporters: list[ReporterOut]
    drugs: list[DrugOut]
    reactions: list[ReactionOut]
    occurrence_country: TextValue | None


class CriterionOut(_Strict):
    met: bool
    quote: str | None
    confidence: float


class CriteriaOut(_Strict):
    death: CriterionOut
    life_threatening: CriterionOut
    hospitalisation: CriterionOut
    disability: CriterionOut
    congenital_anomaly: CriterionOut
    other_medically_important: CriterionOut


class ReactionSeriousnessOut(_Strict):
    reaction_id: str
    criteria: CriteriaOut


class SeriousnessOut(_Strict):
    reactions: list[ReactionSeriousnessOut]


class CodingChoiceOut(_Strict):
    item_id: str
    candidate_id: str | None
    confidence: float


class CodingOut(_Strict):
    choices: list[CodingChoiceOut]


class QuestionOut(_Strict):
    path: str
    question: str


class FollowUpOut(_Strict):
    questions: list[QuestionOut]


class NarrativeOut(_Strict):
    narrative: str


OUTPUT_MODELS: dict[Step, type[_Strict]] = {
    Step.EXTRACTION: ExtractionOut,
    Step.SERIOUSNESS: SeriousnessOut,
    Step.CODING: CodingOut,
    Step.FOLLOW_UP: FollowUpOut,
    Step.NARRATIVE: NarrativeOut,
}

# Variables each step's prompt receives. All are untrusted (derived from report text).
STEP_VARIABLES: dict[Step, frozenset[str]] = {
    Step.EXTRACTION: frozenset({"report"}),
    Step.SERIOUSNESS: frozenset({"report", "reactions"}),
    Step.CODING: frozenset({"items"}),
    Step.FOLLOW_UP: frozenset({"gaps"}),
    Step.NARRATIVE: frozenset({"facts"}),
}


def json_schema(step: Step) -> dict[str, Any]:
    return OUTPUT_MODELS[step].model_json_schema()
