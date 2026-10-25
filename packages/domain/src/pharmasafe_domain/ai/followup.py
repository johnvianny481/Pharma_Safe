"""What is missing from a case: the basis for follow-up questions.

The gaps are found by rules (validity criteria, seriousness not assessed, missing dates,
doses and outcomes). The model only words the questions for the reporter, and may only
ask about the gaps it was given.
"""

from dataclasses import dataclass

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.rules.seriousness import unassessed_criteria
from pharmasafe_domain.rules.validity import Criterion, assess_validity, is_suspect_drug

_VALIDITY_GAPS = {
    Criterion.PATIENT: ("/patient", "an identifier for the patient (initials, age or sex)"),
    Criterion.REPORTER: ("/reporters", "who reported the event and their qualification"),
    Criterion.SUSPECT_DRUG: ("/drugs", "the name of the suspected medicine"),
    Criterion.REACTION: ("/reactions", "what adverse event happened"),
}


@dataclass(frozen=True)
class Gap:
    path: str
    description: str


def missing_information(case: SafetyCase) -> list[Gap]:
    gaps = [
        Gap(*_VALIDITY_GAPS[c])
        for c in sorted(assess_validity(case).missing, key=lambda c: c.value)
    ]
    for reaction in case.reactions:
        base = f"/reactions/{reaction.id}"
        term = reaction.reported_term.value if reaction.reported_term else "the reaction"
        if reaction.onset_date is None:
            gaps.append(Gap(f"{base}/onset_date", f"when {term} started"))
        if reaction.outcome is None:
            gaps.append(Gap(f"{base}/outcome", f"the outcome of {term}"))
        if unassessed_criteria(reaction.seriousness):
            gaps.append(
                Gap(
                    f"{base}/seriousness",
                    f"whether {term} led to death, hospitalisation, disability or was"
                    " life-threatening",
                )
            )
    for drug in case.drugs:
        if not is_suspect_drug(drug):
            continue
        base = f"/drugs/{drug.id}"
        name = drug.product_name.value if drug.product_name else "the suspected medicine"
        if drug.dose is None:
            gaps.append(Gap(f"{base}/dose", f"the dose of {name}"))
        if drug.start_date is None:
            gaps.append(Gap(f"{base}/start_date", f"when {name} was started"))
    return gaps
