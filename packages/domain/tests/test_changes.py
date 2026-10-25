"""Changes and replay. Replaying recorded changes must rebuild a version exactly."""

from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import TypeAdapter

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.changes import (
    AddItem,
    Change,
    ChangeError,
    RemoveItem,
    SetField,
    apply_change,
    replay,
    tracked_value_at,
)
from pharmasafe_domain.provenance import Tracked

T0 = datetime(2026, 10, 1, 9, 0, tzinfo=UTC)


def hv(value: Any) -> dict[str, Any]:
    return Tracked[Any].human(value, by="proc-1", at=T0).model_dump(mode="json")


def test_set_add_and_remove() -> None:
    case = SafetyCase()
    case, old = apply_change(case, SetField(path="/patient/initials", value=hv("JD")))
    assert old is None
    assert case.patient.initials is not None
    assert case.patient.initials.value == "JD"

    case, _ = apply_change(case, AddItem(path="/reactions", item={"id": "r1"}))
    case, _ = apply_change(case, SetField(path="/reactions/r1/seriousness/death", value=hv(True)))
    assert case.reactions[0].seriousness.death is not None

    case, old = apply_change(case, SetField(path="/patient/initials", value=None))
    assert old["value"] == "JD"
    assert case.patient.initials is None

    case, old = apply_change(case, RemoveItem(path="/reactions/r1"))
    assert old["id"] == "r1"
    assert case.reactions == ()


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (SetField(path="/patient", value=hv("x")), "not a tracked field"),
        (SetField(path="/schema_version", value=hv(2)), "not a tracked field"),
        (SetField(path="/patient/nope", value=hv("x")), "not a field"),
        (SetField(path="/reactions/r9/outcome", value=hv("fatal")), "no item with id"),
        (SetField(path="/reactions/r1/id", value=hv("r2")), "not a tracked field"),
        (SetField(path="/reactions/r1/outcome", value=hv("bad-outcome")), "reactions"),
        (SetField(path="/patient/age/value", value=hv(1)), "no such path segment"),
        (AddItem(path="/patient", item={"id": "x"}), "items can only be added"),
        (AddItem(path="/reactions", item={"id": "r1"}), "already has an item"),
        (AddItem(path="/reactions", item={"id": "r2", "colour": "red"}), "reactions"),
        (RemoveItem(path="/patient/initials"), "not a list item"),
        (RemoveItem(path="/reactions/r9"), "no item with id"),
    ],
)
def test_invalid_changes_are_rejected(change: Any, message: str) -> None:
    base = SafetyCase.from_json({"reactions": [{"id": "r1"}]})
    with pytest.raises(ChangeError, match=message):
        apply_change(base, change)


def test_tracked_value_at() -> None:
    case, _ = apply_change(SafetyCase(), SetField(path="/patient/initials", value=hv("JD")))
    assert tracked_value_at(case, "/patient/initials") == hv("JD")
    assert tracked_value_at(case, "/patient/sex") is None
    for path in ("/patient", "/patient/nope", "/reactions"):
        with pytest.raises(ChangeError, match="not a tracked field"):
            tracked_value_at(case, path)


def test_removing_a_referenced_item_is_rejected() -> None:
    base = SafetyCase.from_json(
        {
            "drugs": [{"id": "d1"}],
            "reactions": [{"id": "r1"}],
            "causality": [{"id": "c1", "drug_id": "d1", "reaction_id": "r1"}],
        }
    )
    with pytest.raises(ChangeError, match="missing drug or reaction"):
        apply_change(base, RemoveItem(path="/drugs/d1"))


# ---------------------------------------------------------------- property: replay


ITEM_IDS = st.sampled_from(["a", "b", "c"])
FIELD_EDITS = st.one_of(
    st.builds(lambda v: ("/patient/initials", hv(v)), st.text("ABCDEFGH", min_size=1, max_size=3)),
    st.builds(lambda v: ("/patient/sex", hv(v)), st.sampled_from(["male", "female", "unknown"])),
    st.just(("/patient/sex", None)),
    st.builds(lambda v: ("/narrative/case_narrative", hv(v)), st.text(min_size=1, max_size=20)),
)


@st.composite
def edit_scripts(draw: st.DrawFn) -> list[Any]:
    """Random sequences of edits that are each valid at the point they are made."""
    present: set[str] = set()
    script: list[Any] = []
    for _ in range(draw(st.integers(0, 25))):
        kind = draw(st.sampled_from(["field", "add", "remove", "reaction_field"]))
        item = draw(ITEM_IDS)
        if kind == "field":
            path, value = draw(FIELD_EDITS)
            script.append(SetField(path=path, value=value))
        elif kind == "add" and item not in present:
            present.add(item)
            script.append(AddItem(path="/reactions", item={"id": item}))
        elif kind == "remove" and item in present:
            present.remove(item)
            script.append(RemoveItem(path=f"/reactions/{item}"))
        elif kind == "reaction_field" and item in present:
            term = draw(st.text("abcxyz ", min_size=1, max_size=10))
            script.append(SetField(path=f"/reactions/{item}/reported_term", value=hv(term)))
    return script


@given(edit_scripts())
def test_replaying_recorded_changes_rebuilds_every_intermediate_state(script: list[Any]) -> None:
    states = [SafetyCase()]
    for change in script:
        states.append(apply_change(states[-1], change)[0])
    for n in range(len(script) + 1):
        assert replay(SafetyCase(), script[:n]).to_json() == states[n].to_json()


@given(edit_scripts())
def test_recorded_changes_survive_json_round_trip(script: list[Any]) -> None:
    """Changes are stored as JSON in the audit ledger; the stored form must replay the same."""
    adapter = TypeAdapter(Change)
    stored = [adapter.validate_json(adapter.dump_json(c)) for c in script]
    assert replay(SafetyCase(), stored) == replay(SafetyCase(), script)
