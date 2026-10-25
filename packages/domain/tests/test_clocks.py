"""Regulatory clocks: example rule sets, rule matching, and properties over case histories."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.clocks import (
    ClockRuleSet,
    ClockTracker,
    RuleSetRegistry,
    Snapshot,
    day_zero,
    evaluate_history,
    load_rule_set,
    seed_registry,
)
from pharmasafe_domain.rules.validity import assess_validity

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)
REGISTRY = seed_registry()
ALL_REGULATORS = ("FDA", "EMA", "CDSCO")


def hv(value: Any) -> dict[str, Any]:
    return Tracked[Any].human(value, by="proc-1", at=T0).model_dump(mode="json")


def make_case(
    *,
    receipt: date | None = date(2026, 10, 1),
    valid: bool = True,
    serious: bool = False,
    fatal: bool = False,
    expectedness: str | None = "unexpected",
    report_type: str | None = "spontaneous",
    country: str | None = "US",
) -> SafetyCase:
    seriousness = {"death": hv(fatal), "hospitalisation": hv(serious and not fatal)}
    reaction: dict[str, Any] = {"id": "r1", "reported_term": hv("rash"), "seriousness": seriousness}
    if expectedness:
        reaction["expectedness"] = hv(expectedness)
    admin: dict[str, Any] = {}
    if receipt:
        admin["latest_receipt_date"] = hv(receipt.isoformat())
    if report_type:
        admin["report_type"] = hv(report_type)
    if country:
        admin["occurrence_country"] = hv(country)
    return SafetyCase.from_json(
        {
            "admin": admin,
            "patient": {"initials": hv("JD")} if valid else {},
            "reporters": [{"id": "p1", "qualification": hv("physician")}],
            "drugs": [{"id": "d1", "role": hv("suspect"), "product_name": hv("Acmezol")}],
            "reactions": [reaction],
        }
    )


def due(
    case: SafetyCase, regulators: tuple[str, ...] = ALL_REGULATORS
) -> set[tuple[str, str, int]]:
    """(regulator, rule, days) for every clock a single snapshot starts."""
    found = evaluate_history([Snapshot(1, case)], REGISTRY, regulators)
    return {(o.regulator, o.rule_id, (o.due_date - o.clock_start).days) for o in found}


# ---------------------------------------------------------------- seeded rule sets


def test_seeded_rule_sets_are_marked_as_examples() -> None:
    assert REGISTRY.regulators == {"FDA", "EMA", "CDSCO"}
    for regulator in REGISTRY.regulators:
        rule_set = REGISTRY.in_force(regulator, date(2026, 1, 1))
        assert rule_set.example
        assert "EXAMPLE" in rule_set.notice
        assert "example" in rule_set.version


def test_serious_unexpected_postmarketing_case() -> None:
    assert due(make_case(serious=True)) == {
        ("FDA", "postmarketing-15-day-alert", 15),
        ("EMA", "icsr-serious", 15),
        ("CDSCO", "serious-adr-15-day", 15),
    }


def test_serious_expected_case_is_not_an_fda_alert() -> None:
    assert ("FDA", "postmarketing-15-day-alert", 15) not in due(
        make_case(serious=True, expectedness="expected")
    )


def test_unassessed_expectedness_counts_as_unexpected() -> None:
    assert ("FDA", "postmarketing-15-day-alert", 15) in due(
        make_case(serious=True, expectedness=None)
    )


def test_non_serious_case_is_due_to_ema_only_if_domestic() -> None:
    assert due(make_case(country="DE")) == {("EMA", "icsr-non-serious-eea", 90)}
    assert due(make_case(country="US")) == set()


def test_unknown_country_is_treated_as_domestic_and_foreign() -> None:
    assert due(make_case(country=None)) == {("EMA", "icsr-non-serious-eea", 90)}


def test_fatal_study_case_gets_7_and_15_day_clocks() -> None:
    found = due(make_case(serious=True, fatal=True, report_type="study", country="IN"))
    assert found == {
        ("FDA", "ind-7-day", 7),
        ("FDA", "ind-15-day", 15),
        ("EMA", "susar-7-day", 7),
        ("EMA", "susar-15-day", 15),
        ("CDSCO", "trial-sae-14-day", 14),
    }


def test_unknown_report_type_matches_both_settings() -> None:
    found = {rule for _, rule, _ in due(make_case(serious=True, report_type=None))}
    assert {"postmarketing-15-day-alert", "ind-15-day"} <= found


def test_only_the_tenants_regulators_are_evaluated() -> None:
    assert {r for r, _, _ in due(make_case(serious=True), ("EMA",))} == {"EMA"}


def test_invalid_case_or_missing_receipt_starts_no_clock() -> None:
    assert due(make_case(serious=True, valid=False)) == set()
    assert due(make_case(serious=True, receipt=None)) == set()


# ---------------------------------------------------------------- rule set versions


def _rule_set(version: str, effective: str, days: int) -> ClockRuleSet:
    return ClockRuleSet.model_validate(
        {
            "regulator": "XYZ",
            "version": version,
            "effective_from": effective,
            "example": True,
            "domestic_countries": ["US"],
            "rules": [{"id": "any", "description": "all valid cases", "due_in_days": days}],
        }
    )


def test_rule_set_in_force_on_the_clock_start_applies() -> None:
    registry = RuleSetRegistry([_rule_set("2", "2026-06-01", 10), _rule_set("1", "2026-01-01", 30)])
    assert registry.in_force("XYZ", date(2026, 5, 31)).version == "1"
    assert registry.in_force("XYZ", date(2026, 6, 1)).version == "2"
    with pytest.raises(LookupError):
        registry.in_force("XYZ", date(2025, 12, 31))
    with pytest.raises(LookupError):
        registry.in_force("ABC", date(2026, 1, 1))

    tracker = ClockTracker(registry, ["XYZ"])
    (early,) = tracker.observe(Snapshot(1, make_case(receipt=date(2026, 5, 20))))
    (late,) = tracker.observe(Snapshot(2, make_case(receipt=date(2026, 6, 20))))
    assert (early.rule_set_version, early.due_date) == ("1", date(2026, 6, 19))
    assert (late.rule_set_version, late.due_date) == ("2", date(2026, 6, 30))


def test_rule_sets_are_validated() -> None:
    with pytest.raises(ValueError, match="same date"):
        RuleSetRegistry([_rule_set("1", "2026-01-01", 30), _rule_set("2", "2026-01-01", 10)])
    bad = _rule_set("1", "2026-01-01", 30).model_dump(mode="json")
    bad["rules"] = bad["rules"] * 2
    with pytest.raises(ValidationError, match="unique"):
        ClockRuleSet.model_validate(bad)
    with pytest.raises(ValidationError):
        load_rule_set('{"regulator": "XYZ"}')


# ---------------------------------------------------------------- properties over histories


@st.composite
def histories(draw: st.DrawFn) -> list[Snapshot]:
    """A case's life: states in time order, receipt dates never going back, versions rising."""
    receipt = draw(st.dates(min_value=date(2020, 1, 1), max_value=date(2030, 1, 1)))
    version = 1
    snapshots = []
    for _ in range(draw(st.integers(1, 8))):
        if draw(st.booleans()):
            receipt += timedelta(days=draw(st.integers(0, 40)))
        if draw(st.integers(0, 4)) == 0:
            version += 1
        case = make_case(
            receipt=receipt,
            valid=draw(st.booleans()),
            serious=draw(st.booleans()),
            fatal=draw(st.booleans()),
            expectedness=draw(st.sampled_from([None, "expected", "unexpected", "unknown"])),
            report_type=draw(st.sampled_from([None, "spontaneous", "study", "other"])),
            country=draw(st.sampled_from([None, "US", "DE", "IN", "JP"])),
        )
        snapshots.append(Snapshot(version, case))
    return snapshots


@given(histories())
def test_due_date_is_clock_start_plus_the_rule_period(history: list[Snapshot]) -> None:
    for o in evaluate_history(history, REGISTRY, ALL_REGULATORS):
        rule_set = REGISTRY.in_force(o.regulator, o.clock_start)
        rule = next(r for r in rule_set.rules if r.id == o.rule_id)
        assert o.due_date == o.clock_start + timedelta(days=rule.due_in_days)
        assert o.rule_set_version == rule_set.version


@given(histories())
def test_clocks_never_reset_as_information_arrives(history: list[Snapshot]) -> None:
    """Every clock started by an earlier part of the history survives unchanged."""
    full = {o.key: o for o in evaluate_history(history, REGISTRY, ALL_REGULATORS)}
    for n in range(len(history)):
        for o in evaluate_history(history[:n], REGISTRY, ALL_REGULATORS):
            assert full[o.key] == o


@given(histories())
def test_no_clock_starts_before_day_zero(history: list[Snapshot]) -> None:
    zero = day_zero(history)
    found = evaluate_history(history, REGISTRY, ALL_REGULATORS)
    if zero is None:
        assert found == []
    for o in found:
        assert zero is not None
        assert o.clock_start >= zero


@given(histories())
def test_clocks_start_at_the_receipt_date_of_a_valid_state(history: list[Snapshot]) -> None:
    valid_receipts = {
        (s.case_version, s.case.admin.latest_receipt_date.value)
        for s in history
        if s.case.admin.latest_receipt_date and assess_validity(s.case).valid
    }
    for o in evaluate_history(history, REGISTRY, ALL_REGULATORS):
        assert (o.case_version, o.clock_start) in valid_receipts


@given(histories(), st.data())
def test_resuming_from_stored_clocks_matches_a_full_evaluation(
    history: list[Snapshot], data: st.DataObject
) -> None:
    """The case store persists started clocks and resumes from them on each change."""
    split = data.draw(st.integers(0, len(history)))
    stored = evaluate_history(history[:split], REGISTRY, ALL_REGULATORS)
    tracker = ClockTracker(REGISTRY, ALL_REGULATORS, started=stored)
    for snapshot in history[split:]:
        tracker.observe(snapshot)
    assert tracker.obligations == evaluate_history(history, REGISTRY, ALL_REGULATORS)


def test_day_zero_is_first_valid_receipt() -> None:
    history = [
        Snapshot(1, make_case(receipt=date(2026, 1, 1), valid=False)),
        Snapshot(1, make_case(receipt=date(2026, 1, 5), valid=True)),
        Snapshot(1, make_case(receipt=date(2026, 1, 9), valid=True)),
    ]
    assert day_zero(history) == date(2026, 1, 5)
    assert day_zero(history[:1]) is None


def test_follow_up_that_makes_a_case_serious_starts_a_new_clock() -> None:
    history = [
        Snapshot(1, make_case(receipt=date(2026, 1, 5), country="US")),
        Snapshot(2, make_case(receipt=date(2026, 2, 1), country="US", serious=True)),
    ]
    (alert,) = [o for o in evaluate_history(history, REGISTRY, ["FDA"])]
    assert (alert.case_version, alert.clock_start, alert.due_date) == (
        2,
        date(2026, 2, 1),
        date(2026, 2, 16),
    )
