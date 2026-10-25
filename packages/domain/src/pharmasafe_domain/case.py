"""The safety case, mirroring the main sections of ICH E2B(R3).

Section letters follow the E2B(R3) implementation guide, so that Phase 7 (XML generation)
is a mapping step, not a redesign:

- C.1 administrative information (:class:`AdminInfo`)
- C.2.r primary sources, i.e. reporters (:class:`Reporter`)
- D patient (:class:`Patient`)
- E.i reactions / events (:class:`Reaction`)
- F.r tests and procedures (:class:`TestResult`)
- G.k drugs (:class:`Drug`), G.k.9.i.2.r causality (:class:`CausalityAssessment`)
- H narrative and comments (:class:`Narrative`)

Every data field is a :class:`~pharmasafe_domain.provenance.Tracked` value or ``None``
(not yet known). Repeating sections are lists of items with a stable ``id``, so a change
can name the item it touches even after other items are added or removed.

The model rejects values that are structurally impossible (a negative age, an unknown
code). Values that are possible but suspicious (an age of 190 years, a dose unit we do
not recognise) are accepted and flagged by :mod:`pharmasafe_domain.rules.checks`, so a
reviewer can see and fix them.
"""

from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from pharmasafe_domain.dates import PartialDate
from pharmasafe_domain.provenance import Tracked

SCHEMA_VERSION: Literal[1] = 1

ItemId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
CountryCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{2}$")]
ProductCode = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9._-]{1,64}$")]
MeddraCode = Annotated[str, StringConstraints(pattern=r"^\d{8}$")]
Text = Annotated[str, StringConstraints(min_length=1, max_length=20000)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
NonNegative = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]


class _Section(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


# ---------------------------------------------------------------- C.1 administrative


class ReportType(StrEnum):
    """C.1.3."""

    SPONTANEOUS = "spontaneous"
    STUDY = "study"
    OTHER = "other"
    NOT_AVAILABLE = "not_available"


class AdminInfo(_Section):
    report_type: Tracked[ReportType] | None = None
    # C.1.4: when the sender first received the report.
    initial_receipt_date: Tracked[date] | None = None
    # C.1.5: when the sender received the most recent information in this version.
    latest_receipt_date: Tracked[date] | None = None
    # E.i.9 / C.2.r.3: where the reaction occurred; decides domestic vs foreign rules.
    occurrence_country: Tracked[CountryCode] | None = None


# ---------------------------------------------------------------- C.2.r reporters


class Qualification(StrEnum):
    """C.2.r.4."""

    PHYSICIAN = "physician"
    PHARMACIST = "pharmacist"
    OTHER_HEALTH_PROFESSIONAL = "other_health_professional"
    LAWYER = "lawyer"
    CONSUMER = "consumer"


class Reporter(_Section):
    id: ItemId
    given_name: Tracked[ShortText] | None = None
    family_name: Tracked[ShortText] | None = None
    organisation: Tracked[ShortText] | None = None
    address: Tracked[ShortText] | None = None
    email: Tracked[ShortText] | None = None
    telephone: Tracked[ShortText] | None = None
    country: Tracked[CountryCode] | None = None
    qualification: Tracked[Qualification] | None = None


# ---------------------------------------------------------------- D patient


class Sex(StrEnum):
    """D.5."""

    MALE = "male"
    FEMALE = "female"
    UNKNOWN = "unknown"


class AgeUnit(StrEnum):
    """D.2.2b, UCUM codes."""

    DECADE = "{decade}"
    YEAR = "a"
    MONTH = "mo"
    WEEK = "wk"
    DAY = "d"
    HOUR = "h"


class Age(_Section):
    value: NonNegative
    unit: AgeUnit


class AgeGroup(StrEnum):
    """D.2.3."""

    FOETUS = "foetus"
    NEONATE = "neonate"
    INFANT = "infant"
    CHILD = "child"
    ADOLESCENT = "adolescent"
    ADULT = "adult"
    ELDERLY = "elderly"


class Patient(_Section):
    # D.1: name or initials. Stored as initials; full names are not needed for safety.
    initials: Tracked[ShortText] | None = None
    # D.1.1.x: a record number (GP, specialist, hospital or investigation).
    record_number: Tracked[ShortText] | None = None
    date_of_birth: Tracked[PartialDate] | None = None
    age: Tracked[Age] | None = None
    age_group: Tracked[AgeGroup] | None = None
    sex: Tracked[Sex] | None = None
    weight_kg: Tracked[NonNegative] | None = None
    height_cm: Tracked[NonNegative] | None = None


# ---------------------------------------------------------------- E.i reactions


class Outcome(StrEnum):
    """E.i.7."""

    RECOVERED = "recovered"
    RECOVERING = "recovering"
    NOT_RECOVERED = "not_recovered"
    RECOVERED_WITH_SEQUELAE = "recovered_with_sequelae"
    FATAL = "fatal"
    UNKNOWN = "unknown"


class Expectedness(StrEnum):
    """Listedness against the reference safety information. Unknown counts as unexpected."""

    EXPECTED = "expected"
    UNEXPECTED = "unexpected"
    UNKNOWN = "unknown"


class Seriousness(_Section):
    """E.i.3.2a-f (ICH E2A). Each criterion is its own field with its own evidence.

    ``None`` means not yet assessed, ``False`` means assessed and not met.
    """

    death: Tracked[bool] | None = None
    life_threatening: Tracked[bool] | None = None
    hospitalisation: Tracked[bool] | None = None
    disability: Tracked[bool] | None = None
    congenital_anomaly: Tracked[bool] | None = None
    other_medically_important: Tracked[bool] | None = None


class Reaction(_Section):
    id: ItemId
    reported_term: Tracked[ShortText] | None = None  # E.i.1.1a
    meddra_code: Tracked[MeddraCode] | None = None  # E.i.2.1b, LLT
    onset_date: Tracked[PartialDate] | None = None  # E.i.4
    end_date: Tracked[PartialDate] | None = None  # E.i.5
    outcome: Tracked[Outcome] | None = None
    seriousness: Seriousness = Seriousness()
    expectedness: Tracked[Expectedness] | None = None


# ---------------------------------------------------------------- F.r tests


class TestResult(_Section):
    __test__ = False  # not a pytest test class

    id: ItemId
    test_name: Tracked[ShortText] | None = None
    test_date: Tracked[PartialDate] | None = None
    result_value: Tracked[Decimal] | None = None
    result_unit: Tracked[ShortText] | None = None
    result_text: Tracked[Text] | None = None
    normal_low: Tracked[Decimal] | None = None
    normal_high: Tracked[Decimal] | None = None


# ---------------------------------------------------------------- G.k drugs


class DrugRole(StrEnum):
    """G.k.1."""

    SUSPECT = "suspect"
    CONCOMITANT = "concomitant"
    INTERACTING = "interacting"
    NOT_ADMINISTERED = "not_administered"


class ActionTaken(StrEnum):
    """G.k.8."""

    WITHDRAWN = "withdrawn"
    DOSE_REDUCED = "dose_reduced"
    DOSE_INCREASED = "dose_increased"
    DOSE_NOT_CHANGED = "dose_not_changed"
    UNKNOWN = "unknown"
    NOT_APPLICABLE = "not_applicable"


class Dose(_Section):
    value: NonNegative
    unit: Annotated[str, StringConstraints(min_length=1, max_length=50)]  # UCUM


class Drug(_Section):
    id: ItemId
    role: Tracked[DrugRole] | None = None
    product_name: Tracked[ShortText] | None = None  # G.k.2.2, as reported
    # The marketing company's own product code; drives data scopes.
    product_code: Tracked[ProductCode] | None = None
    active_substance: Tracked[ShortText] | None = None  # G.k.2.3
    dose: Tracked[Dose] | None = None  # G.k.4.r.1
    route: Tracked[ShortText] | None = None
    start_date: Tracked[PartialDate] | None = None
    end_date: Tracked[PartialDate] | None = None
    batch_number: Tracked[ShortText] | None = None
    indication: Tracked[ShortText] | None = None
    action_taken: Tracked[ActionTaken] | None = None


class CausalityAssessment(_Section):
    """G.k.9.i.2.r: one assessment of whether one drug caused one reaction."""

    id: ItemId
    drug_id: ItemId
    reaction_id: ItemId
    assessor: Tracked[ShortText] | None = None  # source of assessment, e.g. reporter, company
    method: Tracked[ShortText] | None = None
    result: Tracked[ShortText] | None = None


# ---------------------------------------------------------------- H narrative


class Narrative(_Section):
    case_narrative: Tracked[Text] | None = None  # H.1
    reporter_comments: Tracked[Text] | None = None  # H.2
    sender_comments: Tracked[Text] | None = None  # H.4


# ---------------------------------------------------------------- the case


ITEM_SECTIONS = ("reporters", "drugs", "reactions", "tests", "causality")


class SafetyCase(_Section):
    schema_version: Literal[1] = SCHEMA_VERSION
    admin: AdminInfo = AdminInfo()
    reporters: tuple[Reporter, ...] = ()
    patient: Patient = Patient()
    reactions: tuple[Reaction, ...] = ()
    tests: tuple[TestResult, ...] = ()
    drugs: tuple[Drug, ...] = ()
    causality: tuple[CausalityAssessment, ...] = ()
    narrative: Narrative = Narrative()

    @model_validator(mode="after")
    def _item_ids_are_unique_and_links_resolve(self) -> Self:
        for section in ITEM_SECTIONS:
            ids = [item.id for item in getattr(self, section)]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{section} item ids must be unique")
        drug_ids = {d.id for d in self.drugs}
        reaction_ids = {r.id for r in self.reactions}
        for assessment in self.causality:
            if assessment.drug_id not in drug_ids or assessment.reaction_id not in reaction_ids:
                raise ValueError(f"causality {assessment.id} refers to a missing drug or reaction")
        return self

    def to_json(self) -> dict[str, Any]:
        """The exact JSON form that is stored, hashed and signed."""
        return self.model_dump(mode="json")

    @classmethod
    def from_json(cls, data: Any) -> "SafetyCase":
        return cls.model_validate(data)


def normalise(data: Any) -> dict[str, Any]:
    """Validate stored JSON and return it in canonical form, ready to hash.

    The database may hand back JSON that differs in insignificant ways (number formats,
    key order). Hashing the canonical form makes those differences irrelevant.
    """
    return SafetyCase.from_json(data).to_json()
