"""Case validity: the four minimum criteria (ICH E2D, EU GVP VI).

A case is valid when it has all of:

1. an identifiable patient: any of initials, record number, date of birth, age, age
   group or sex;
2. an identifiable reporter: a reporter with any of a name, organisation, contact
   detail (address, email, telephone) or qualification;
3. at least one suspect (or interacting) drug, named by product or active substance;
4. at least one adverse reaction with a reported term.

A value counts whatever its source: AI proposals are enough to triage a case as valid,
and approval separately requires every value to be confirmed by a person.
"""

from dataclasses import dataclass
from enum import StrEnum

from pharmasafe_domain.case import Drug, DrugRole, Patient, Reaction, Reporter, SafetyCase


class Criterion(StrEnum):
    PATIENT = "identifiable_patient"
    REPORTER = "identifiable_reporter"
    SUSPECT_DRUG = "suspect_drug"
    REACTION = "adverse_reaction"


@dataclass(frozen=True)
class ValidityResult:
    missing: frozenset[Criterion]

    @property
    def valid(self) -> bool:
        return not self.missing


def assess_validity(case: SafetyCase) -> ValidityResult:
    met = {
        Criterion.PATIENT: patient_identifiable(case.patient),
        Criterion.REPORTER: any(reporter_identifiable(r) for r in case.reporters),
        Criterion.SUSPECT_DRUG: any(is_suspect_drug(d) for d in case.drugs),
        Criterion.REACTION: any(reaction_identified(r) for r in case.reactions),
    }
    return ValidityResult(missing=frozenset(c for c, ok in met.items() if not ok))


def patient_identifiable(patient: Patient) -> bool:
    return any(
        field is not None
        for field in (
            patient.initials,
            patient.record_number,
            patient.date_of_birth,
            patient.age,
            patient.age_group,
            patient.sex,
        )
    )


def reporter_identifiable(reporter: Reporter) -> bool:
    return any(
        field is not None
        for field in (
            reporter.given_name,
            reporter.family_name,
            reporter.organisation,
            reporter.address,
            reporter.email,
            reporter.telephone,
            reporter.qualification,
        )
    )


def is_suspect_drug(drug: Drug) -> bool:
    if drug.role is None or drug.role.value not in (DrugRole.SUSPECT, DrugRole.INTERACTING):
        return False
    return drug.product_name is not None or drug.active_substance is not None


def reaction_identified(reaction: Reaction) -> bool:
    return reaction.reported_term is not None or reaction.meddra_code is not None
