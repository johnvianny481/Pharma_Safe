"""Redaction: identifiers become reversible tokens; clinical facts stay readable."""

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_domain.ai.redaction import TOKEN, Kind, Redaction, find_identifiers, redact

REPORT = (
    "Reporter: Dr Maria Silva (silva@clinic.example, +44 20 7946 0958), 12 Baker Street.\n"
    "Patient John Smith (MRN: A123456, DOB: 12/03/1961), a 64-year-old man, patient JD,\n"
    "developed a severe rash on 2026-10-01 after Acmezol 20 mg; admitted 01.10.2026 to ICU."
)


def test_direct_identifiers_are_replaced_by_tokens() -> None:
    r = redact(REPORT)
    for original in ("Maria Silva", "silva@clinic.example", "+44 20 7946 0958",
                     "12 Baker Street", "John Smith", "A123456", "12/03/1961"):  # fmt: skip
        assert original not in r.text
        assert original in r.tokens.values()
    assert {k.split("_")[0].strip("[") for k in r.tokens} == {
        "NAME", "EMAIL", "PHONE", "ADDRESS", "ID", "DOB",
    }  # fmt: skip


def test_clinical_facts_and_initials_are_kept() -> None:
    r = redact(REPORT)
    for kept in ("Dr ", "64-year-old man", "patient JD", "severe rash", "2026-10-01",
                 "Acmezol 20 mg", "01.10.2026", "ICU"):  # fmt: skip
        assert kept in r.text


def test_the_same_value_gets_the_same_token() -> None:
    r = redact("Dr Maria Silva called. Later Dr Maria Silva wrote again.")
    assert r.text.count("[NAME_1]") == 2
    assert len(r.tokens) == 1


def test_known_values_are_redacted_wherever_they_appear() -> None:
    r = redact("Silva confirmed the dose; silva also sent photos.", known=["Silva"])
    assert "Silva" not in r.text
    assert "silva" not in r.text


@pytest.mark.parametrize(
    "text",
    [
        "Herr Thomas Müller-Lüdenscheid berichtet",
        "Mme Claire Dubois a signalé",
        "Sig.ra Giulia Rossi ha segnalato",
        "Name: Ana García",
        "the pharmacist Peter O'Brien called",
    ],
)
def test_names_after_cues_in_several_languages(text: str) -> None:
    assert [f.kind for f in find_identifiers(text)] == [Kind.NAME]


def test_words_after_cues_that_are_not_names_are_kept() -> None:
    assert find_identifiers("Patient Information: patient developed hives") == []
    assert find_identifiers("Dr Monday") == []


def test_dates_and_doses_are_not_phone_numbers() -> None:
    assert find_identifiers("started 2026-10-01, stopped 01.10.2026, dose 100 200 mg") == []


def test_tokens_already_in_the_text_are_left_alone() -> None:
    assert find_identifiers("Dr [NAME_1] at [EMAIL_1]") == []


def test_restore_and_offsets_map_back_to_the_original() -> None:
    r = redact(REPORT)
    assert r.restore(r.text) == REPORT
    start = r.text.index("Acmezol")
    a, b = r.to_original(start, start + len("Acmezol 20 mg"))
    assert REPORT[a:b] == "Acmezol 20 mg"
    token = r.text.index("[NAME_2]")
    a, b = r.to_original(token, token + len("[NAME_2]"))
    assert REPORT[a:b] == "John Smith"
    a, b = r.to_original(token - 8, token + 3)  # a span that cuts into a token
    assert REPORT[a:b] == "Patient John Smith"


def test_to_original_refuses_spans_outside_the_text() -> None:
    with pytest.raises(ValueError, match="outside"):
        redact("abc").to_original(0, 10)


def test_round_trip_through_storage() -> None:
    r = redact(REPORT)
    assert Redaction.from_json(r.to_json()) == r


_TEXT = st.text(
    alphabet=st.sampled_from(list("abc XYZ Dr Mr patient 0123456789 .,:-@+\n")), max_size=200
)


@given(_TEXT)
def test_redaction_is_always_reversible(text: str) -> None:
    r = redact(text)
    assert r.restore(r.text) == text
    assert all(TOKEN.fullmatch(t) for t in r.tokens)
    # Every plain character maps back to the same character in the original.
    for seg in r.segments:
        if not seg.token:
            assert r.text[seg.red_start : seg.red_end] == text[seg.orig_start : seg.orig_end]
