"""Property-based tests for the four minimum criteria."""

from datetime import UTC, datetime
from typing import Any

from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.validity import Criterion, assess_validity

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)

PATIENT_FIELDS = {
    "initials": "JD",
    "record_number": "MRN-1",
    "date_of_birth": "1980",
    "age": {"value": "42", "unit": "a"},
    "age_group": "adult",
    "sex": "female",
}
NON_IDENTIFYING_PATIENT_FIELDS = {"weight_kg": "70", "height_cm": "170"}
REPORTER_FIELDS = {
    "given_name": "Ana",
    "family_name": "Silva",
    "organisation": "City Hospital",
    "address": "1 Main St",
    "email": "a@example.org",
    "telephone": "+100",
    "qualification": "physician",
}
DRUG_NAME_FIELDS = {"product_name": "Acmezol", "active_substance": "acmezolam"}
REACTION_FIELDS = {"reported_term": "rash", "meddra_code": "10037844"}


def hv(value: Any) -> dict[str, Any]:
    return Tracked[Any].human(value, by="proc-1", at=T0).model_dump(mode="json")


def subset(fields: dict[str, Any]) -> st.SearchStrategy[dict[str, Any]]:
    return st.sets(st.sampled_from(sorted(fields))).map(
        lambda keys: {k: hv(fields[k]) for k in keys}
    )


@st.composite
def cases(draw: st.DrawFn) -> SafetyCase:
    patient = draw(subset(PATIENT_FIELDS)) | draw(subset(NON_IDENTIFYING_PATIENT_FIELDS))
    reporters = [
        {"id": f"p{i}", **draw(subset(REPORTER_FIELDS)), "country": hv("DE")}
        for i in range(draw(st.integers(0, 2)))
    ]
    drugs = []
    for i in range(draw(st.integers(0, 3))):
        role = draw(
            st.sampled_from([None, "suspect", "interacting", "concomitant", "not_administered"])
        )
        drug = {
            "id": f"d{i}",
            **draw(subset(DRUG_NAME_FIELDS)),
            "dose": hv({"value": "5", "unit": "mg"}),
        }
        if role:
            drug["role"] = hv(role)
        drugs.append(drug)
    reactions = [
        {"id": f"r{i}", **draw(subset(REACTION_FIELDS)), "outcome": hv("recovered")}
        for i in range(draw(st.integers(0, 2)))
    ]  # fmt: skip
    return SafetyCase.from_json(
        {"patient": patient, "reporters": reporters, "drugs": drugs, "reactions": reactions}
    )


def oracle(case: SafetyCase) -> set[Criterion]:
    """An independent statement of the rule, written from the data rather than the code."""
    data = case.to_json()
    missing = set()
    if not any(data["patient"][k] for k in PATIENT_FIELDS):
        missing.add(Criterion.PATIENT)
    if not any(any(r[k] for k in REPORTER_FIELDS) for r in data["reporters"]):
        missing.add(Criterion.REPORTER)
    if not any(
        d["role"] and d["role"]["value"] in ("suspect", "interacting") and any(d[k] for k in DRUG_NAME_FIELDS)
        for d in data["drugs"]
    ):  # fmt: skip
        missing.add(Criterion.SUSPECT_DRUG)
    if not any(any(r[k] for k in REACTION_FIELDS) for r in data["reactions"]):
        missing.add(Criterion.REACTION)
    return missing


@given(cases())
def test_validity_matches_the_four_criteria(case: SafetyCase) -> None:
    result = assess_validity(case)
    assert result.missing == oracle(case)
    assert result.valid == (not result.missing)


@given(cases(), cases())
def test_more_information_never_makes_a_case_invalid(a: SafetyCase, b: SafetyCase) -> None:
    """Merging in another report's sections can only meet more criteria, never fewer."""
    merged = SafetyCase.from_json(
        {
            "patient": {k: a.to_json()["patient"][k] or b.to_json()["patient"][k] for k in a.to_json()["patient"]},
            "reporters": [*a.to_json()["reporters"], *({**r, "id": "b" + r["id"]} for r in b.to_json()["reporters"])],
            "drugs": [*a.to_json()["drugs"], *({**d, "id": "b" + d["id"]} for d in b.to_json()["drugs"])],
            "reactions": [*a.to_json()["reactions"], *({**r, "id": "b" + r["id"]} for r in b.to_json()["reactions"])],
        }
    )  # fmt: skip
    assert assess_validity(merged).missing <= assess_validity(a).missing


def test_empty_case_misses_everything() -> None:
    assert assess_validity(SafetyCase()).missing == set(Criterion)


def test_concomitant_drug_alone_is_not_enough() -> None:
    case = SafetyCase.from_json(
        {"drugs": [{"id": "d1", "role": hv("concomitant"), "product_name": hv("X")}]}
    )
    assert Criterion.SUSPECT_DRUG in assess_validity(case).missing
