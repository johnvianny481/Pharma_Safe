from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import pytest

from pharmasafe_domain.case import Age, AgeUnit, SafetyCase
from pharmasafe_domain.provenance import EvidenceSpan, Source, Tracked
from pharmasafe_domain.rules.checks import (
    Severity,
    age_in_years,
    approval_blockers,
    check_case,
    tracked_fields,
)

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
TODAY = date(2026, 10, 10)


def hv(value: Any) -> dict[str, Any]:
    return Tracked[Any].human(value, by="proc-1", at=T0).model_dump(mode="json")


def ai(value: Any) -> dict[str, Any]:
    span = EvidenceSpan(document_id="doc", start=0, end=1, text="x")
    return Tracked[Any](value=value, source=Source.AI, evidence=span, confidence=0.8).model_dump(
        mode="json"
    )


def codes(case: dict[str, Any]) -> set[str]:
    return {i.code for i in check_case(SafetyCase.from_json(case), today=TODAY)}


ALL_ASSESSED = {
    k: hv(False)
    for k in ("death", "life_threatening", "hospitalisation", "disability", "congenital_anomaly",
              "other_medically_important")
}  # fmt: skip

VALID: dict[str, Any] = {
    "admin": {
        "report_type": hv("spontaneous"),
        "initial_receipt_date": hv("2026-10-01"),
        "latest_receipt_date": hv("2026-10-01"),
        "occurrence_country": hv("DE"),
    },
    "patient": {"initials": hv("JD"), "date_of_birth": hv("1980-05")},
    "reporters": [{"id": "p1", "qualification": hv("physician")}],
    "drugs": [{"id": "d1", "role": hv("suspect"), "product_name": hv("Acmezol"),
               "start_date": hv("2026-09-01"), "dose": hv({"value": "5", "unit": "mg"})}],
    "reactions": [{"id": "r1", "reported_term": hv("rash"), "onset_date": hv("2026-09-03"),
                   "seriousness": ALL_ASSESSED}],
}  # fmt: skip


def with_(path: str, value: Any) -> dict[str, Any]:
    """A copy of VALID with one value replaced (path like 'drugs.0.end_date')."""
    data = SafetyCase.from_json(VALID).to_json()
    *parents, last = path.split(".")
    node: Any = data
    for key in parents:
        node = node[int(key)] if key.isdigit() else node[key]
    node[last] = value
    return data


def test_valid_case_has_no_issues_and_can_be_approved() -> None:
    case = SafetyCase.from_json(VALID)
    assert check_case(case, today=TODAY) == []
    assert approval_blockers(case, today=TODAY) == []


@pytest.mark.parametrize(
    ("path", "value", "code"),
    [
        ("reactions.0.onset_date", hv("2026-10-05"), "future_date"),  # after receipt
        ("patient.date_of_birth", hv("1850"), "implausible_date"),
        ("admin.initial_receipt_date", hv("2026-10-11"), "future_date"),  # after today
        ("admin.initial_receipt_date", hv("2026-10-02"), "receipt_dates_reversed"),
        ("reactions.0.end_date", hv("2026-09-02"), "end_before_start"),
        ("drugs.0.end_date", hv("2026-08"), "end_before_start"),
        ("patient.date_of_birth", hv("2026-09-20"), "before_birth"),
        ("patient.age", hv({"value": "131", "unit": "a"}), "implausible_age"),
        ("patient.weight_kg", hv("900"), "implausible_weight"),
        ("patient.height_cm", hv("5"), "implausible_height"),
        ("drugs.0.dose", hv({"value": "5", "unit": "mgs"}), "unknown_dose_unit"),
    ],
)
def test_each_check_flags_its_problem(path: str, value: Any, code: str) -> None:
    assert code in codes(with_(path, value))


def test_partial_dates_are_flagged_only_when_certainly_wrong() -> None:
    # Onset "2026-09" and drug start 2026-09-01: may be fine, so no issue.
    assert codes(with_("reactions.0.onset_date", hv("2026-09"))) == set()
    assert codes(with_("reactions.0.end_date", hv("2026-09"))) == set()


def test_test_result_checks() -> None:
    data = with_("tests", [{"id": "t1", "test_name": hv("ALT"), "result_value": hv("80"),
                           "normal_low": hv("50"), "normal_high": hv("10"), "test_date": hv("2026-09-04")}])  # fmt: skip
    issues = {i.code: i for i in check_case(SafetyCase.from_json(data), today=TODAY)}
    assert issues["missing_unit"].severity is Severity.WARNING
    assert issues["range_reversed"].severity is Severity.ERROR
    blockers = {i.code for i in approval_blockers(SafetyCase.from_json(data), today=TODAY)}
    assert "missing_unit" not in blockers  # warnings do not block
    assert "range_reversed" in blockers


def test_unconfirmed_ai_values_block_approval() -> None:
    data = with_("patient.sex", ai("female"))
    blockers = approval_blockers(SafetyCase.from_json(data), today=TODAY)
    assert [(b.code, b.path) for b in blockers] == [("unconfirmed_value", "/patient/sex")]


def test_invalid_or_unassessed_cases_cannot_be_approved() -> None:
    data = with_("reactions.0.seriousness", {})
    data["reporters"] = []
    data["admin"]["latest_receipt_date"] = None
    blockers = {b.code for b in approval_blockers(SafetyCase.from_json(data), today=TODAY)}
    assert {"invalid_case", "seriousness_not_assessed", "missing_receipt_date"} <= blockers


def test_tracked_fields_lists_every_value_with_its_path() -> None:
    paths = [p for p, _ in tracked_fields(SafetyCase.from_json(VALID))]
    assert "/patient/initials" in paths
    assert "/reactions/r1/seriousness/death" in paths
    assert "/drugs/d1/dose" in paths
    assert len(paths) == len(set(paths))


@pytest.mark.parametrize(
    ("age", "years"),
    [
        (Age(value=Decimal(3), unit=AgeUnit.DECADE), Decimal(30)),
        (Age(value=Decimal(24), unit=AgeUnit.MONTH), Decimal(2)),
        (Age(value=Decimal("365.25"), unit=AgeUnit.DAY), Decimal(1)),
    ],
)
def test_age_conversion(age: Age, years: Decimal) -> None:
    assert age_in_years(age) == years
