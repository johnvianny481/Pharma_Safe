"""Date and unit checks, and what blocks approval.

Checks return :class:`Issue` objects rather than raising, so the review screen can show
every problem at once. ``error`` issues block approval; ``warning`` issues ask a person
to look but do not block.

Date checks flag only definite contradictions (see :mod:`pharmasafe_domain.dates`): an
onset in "2026-03" and a drug start on "2026-03-15" might be fine and are not flagged.
"""

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel

from pharmasafe_domain.case import Age, AgeUnit, SafetyCase
from pharmasafe_domain.dates import definitely_before
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.seriousness import unassessed_criteria
from pharmasafe_domain.rules.validity import assess_validity

# Earliest date accepted anywhere in a case; older values are almost always typos.
MIN_PLAUSIBLE_DATE = date(1900, 1, 1)

# UCUM units accepted for doses. Extend through configuration, not by guessing.
DOSE_UNITS = frozenset(
    {"kg", "g", "mg", "ug", "ng", "L", "mL", "uL", "[iU]", "[kIU]", "[MIU]", "mmol", "umol",
     "mg/kg", "ug/kg", "mg/m2", "{tbl}", "{cap}", "{drp}", "{puff}", "{dose}"}
)  # fmt: skip

MAX_AGE_YEARS = Decimal(130)
DAYS_PER_UNIT = {
    AgeUnit.DECADE: Decimal("3652.5"),
    AgeUnit.YEAR: Decimal("365.25"),
    AgeUnit.MONTH: Decimal("30.4375"),
    AgeUnit.WEEK: Decimal(7),
    AgeUnit.DAY: Decimal(1),
    AgeUnit.HOUR: Decimal(1) / Decimal(24),
}
WEIGHT_KG_RANGE = (Decimal("0.2"), Decimal(700))
HEIGHT_CM_RANGE = (Decimal(20), Decimal(280))


class Severity(StrEnum):
    ERROR = "error"
    WARNING = "warning"


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str
    severity: Severity = Severity.ERROR


def check_case(case: SafetyCase, *, today: date) -> list[Issue]:
    """All date and unit issues. ``today`` is passed in so results are reproducible."""
    return [*_date_issues(case, today), *_unit_issues(case)]


def approval_blockers(case: SafetyCase, *, today: date) -> list[Issue]:
    """Why this version cannot be approved yet. Empty means it can be."""
    blockers = [i for i in check_case(case, today=today) if i.severity is Severity.ERROR]
    validity = assess_validity(case)
    blockers += [
        Issue("invalid_case", "/", f"minimum criterion missing: {c.value}")
        for c in sorted(validity.missing)
    ]
    if case.admin.latest_receipt_date is None:
        blockers.append(
            Issue("missing_receipt_date", "/admin/latest_receipt_date", "receipt date is needed")
        )
    blockers += [
        Issue("unconfirmed_value", path, "a person must confirm this value")
        for path, field in tracked_fields(case)
        if not field.confirmed
    ]
    for reaction in case.reactions:
        for name in unassessed_criteria(reaction.seriousness):
            blockers.append(
                Issue(
                    "seriousness_not_assessed",
                    f"/reactions/{reaction.id}/seriousness/{name}",
                    "every seriousness criterion must be assessed",
                )
            )
    return blockers


def tracked_fields(node: BaseModel, path: str = "") -> Iterator[tuple[str, Tracked[Any]]]:
    """Every tracked value in the case with its change path, in a stable order."""
    for name in type(node).model_fields:
        value = getattr(node, name)
        if isinstance(value, Tracked):
            yield f"{path}/{name}", value
        elif isinstance(value, BaseModel):
            yield from tracked_fields(value, f"{path}/{name}")
        elif isinstance(value, tuple):
            for item in value:
                yield from tracked_fields(item, f"{path}/{name}/{item.id}")


# ---------------------------------------------------------------- dates


def _date_issues(case: SafetyCase, today: date) -> Iterator[Issue]:
    admin = case.admin
    initial = admin.initial_receipt_date.value if admin.initial_receipt_date else None
    latest = admin.latest_receipt_date.value if admin.latest_receipt_date else None
    # Nothing in a report can happen after the company received it, or after today.
    horizon = min(d for d in (latest, today) if d is not None)

    for path, value in _dates(case):
        # Receipt dates are checked against today only; their order is checked below.
        limit = today if path.startswith("/admin/") else horizon
        if definitely_before(value, MIN_PLAUSIBLE_DATE):
            yield Issue("implausible_date", path, "date is before 1900")
        elif definitely_before(limit, value):
            yield Issue("future_date", path, f"date is after {limit.isoformat()}")

    if initial and latest and latest < initial:
        yield Issue(
            "receipt_dates_reversed",
            "/admin/latest_receipt_date",
            "latest receipt date is before the initial receipt date",
        )

    birth = case.patient.date_of_birth.value if case.patient.date_of_birth else None
    for r in case.reactions:
        base = f"/reactions/{r.id}"
        onset = r.onset_date.value if r.onset_date else None
        end = r.end_date.value if r.end_date else None
        if onset and end and definitely_before(end, onset):
            yield Issue("end_before_start", f"{base}/end_date", "reaction ends before its onset")
        if onset and birth and definitely_before(onset, birth):
            yield Issue("before_birth", f"{base}/onset_date", "reaction onset is before birth")
    for d in case.drugs:
        start = d.start_date.value if d.start_date else None
        end = d.end_date.value if d.end_date else None
        if start and end and definitely_before(end, start):
            yield Issue("end_before_start", f"/drugs/{d.id}/end_date", "drug ends before it starts")


def _dates(case: SafetyCase) -> Iterator[tuple[str, str | date]]:
    admin = case.admin
    for name in ("initial_receipt_date", "latest_receipt_date"):
        field = getattr(admin, name)
        if field is not None:
            yield f"/admin/{name}", field.value
    if case.patient.date_of_birth:
        yield "/patient/date_of_birth", case.patient.date_of_birth.value
    for r in case.reactions:
        for name in ("onset_date", "end_date"):
            if (field := getattr(r, name)) is not None:
                yield f"/reactions/{r.id}/{name}", field.value
    for d in case.drugs:
        for name in ("start_date", "end_date"):
            if (field := getattr(d, name)) is not None:
                yield f"/drugs/{d.id}/{name}", field.value
    for t in case.tests:
        if t.test_date is not None:
            yield f"/tests/{t.id}/test_date", t.test_date.value


# ---------------------------------------------------------------- units and ranges


def _unit_issues(case: SafetyCase) -> Iterator[Issue]:
    patient = case.patient
    if patient.age is not None and age_in_years(patient.age.value) > MAX_AGE_YEARS:
        yield Issue("implausible_age", "/patient/age", f"age is over {MAX_AGE_YEARS} years")
    if patient.weight_kg is not None and not _within(patient.weight_kg.value, WEIGHT_KG_RANGE):
        yield Issue("implausible_weight", "/patient/weight_kg", "weight is outside 0.2-700 kg")
    if patient.height_cm is not None and not _within(patient.height_cm.value, HEIGHT_CM_RANGE):
        yield Issue("implausible_height", "/patient/height_cm", "height is outside 20-280 cm")
    for d in case.drugs:
        if d.dose is not None and d.dose.value.unit not in DOSE_UNITS:
            yield Issue(
                "unknown_dose_unit", f"/drugs/{d.id}/dose", f"unknown unit {d.dose.value.unit!r}"
            )
    for t in case.tests:
        if t.result_value is not None and t.result_unit is None:
            yield Issue(
                "missing_unit",
                f"/tests/{t.id}/result_unit",
                "numeric result has no unit",
                Severity.WARNING,
            )
        low, high = t.normal_low, t.normal_high
        if low is not None and high is not None and low.value > high.value:
            yield Issue("range_reversed", f"/tests/{t.id}/normal_low", "normal low is above high")


def age_in_years(age: Age) -> Decimal:
    return age.value * DAYS_PER_UNIT[age.unit] / DAYS_PER_UNIT[AgeUnit.YEAR]


def _within(value: Decimal, bounds: tuple[Decimal, Decimal]) -> bool:
    return bounds[0] <= value <= bounds[1]
