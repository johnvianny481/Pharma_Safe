from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from pydantic import ValidationError

from pharmasafe_domain.case import (
    Age,
    AgeUnit,
    CausalityAssessment,
    Drug,
    Patient,
    Reaction,
    SafetyCase,
    normalise,
)
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.regions import region_for_country

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def h[T](value: T) -> Tracked[T]:
    return Tracked[T].human(value, by="proc-1", at=T0)


def test_json_round_trip_is_exact() -> None:
    case = SafetyCase(
        patient=Patient(age=h(Age(value=Decimal("42.50"), unit=AgeUnit.YEAR)), weight_kg=h(Decimal("70"))),
        reactions=(Reaction(id="r1", reported_term=h("rash"), onset_date=h("2026-09")),),
    )  # fmt: skip
    data = case.to_json()
    assert SafetyCase.from_json(data) == case
    assert data["patient"]["age"]["value"]["value"] == "42.50"  # decimals stay exact
    assert normalise(data) == data


def test_normalise_ignores_key_order_and_number_spelling() -> None:
    data = SafetyCase(patient=Patient(weight_kg=h(Decimal("70.5")))).to_json()
    shuffled = dict(reversed(list(data.items())))
    shuffled["patient"] = {
        **data["patient"],
        "weight_kg": {**data["patient"]["weight_kg"], "value": 70.5},
    }
    assert normalise(shuffled) == data


def test_unknown_fields_are_rejected() -> None:
    data = SafetyCase().to_json()
    data["patient"]["shoe_size"] = None
    with pytest.raises(ValidationError):
        SafetyCase.from_json(data)


def test_item_ids_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="unique"):
        SafetyCase(reactions=(Reaction(id="r1"), Reaction(id="r1")))


def test_causality_must_reference_existing_items() -> None:
    with pytest.raises(ValidationError, match="missing drug or reaction"):
        SafetyCase(
            drugs=(Drug(id="d1"),),
            causality=(CausalityAssessment(id="c1", drug_id="d1", reaction_id="r9"),),
        )


def test_structurally_impossible_values_are_rejected() -> None:
    with pytest.raises(ValidationError):
        Age(value=Decimal(-1), unit=AgeUnit.YEAR)
    with pytest.raises(ValidationError):
        Reaction(id="r1", onset_date=h("2026-13"))
    with pytest.raises(ValidationError):
        Reaction(id="bad id!")
    with pytest.raises(ValidationError):
        SafetyCase(
            admin={"initial_receipt_date": h(date(2026, 1, 1)), "occurrence_country": h("usa")}
        )  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("country", "region"),
    [("DE", "eu"), ("NO", "eu"), ("US", "us"), ("IN", "in"), ("JP", "other"), (None, "unknown")],
)
def test_region_for_country(country: str | None, region: str) -> None:
    assert region_for_country(country) == region
