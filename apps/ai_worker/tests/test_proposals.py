"""Model output to case proposals: the evidence rule, coded values, and coding by retrieval."""

from datetime import UTC, datetime
from typing import Any

import pytest

from pharmasafe_ai_worker.dictionary import Kind, SyntheticDictionary, similarity
from pharmasafe_ai_worker.proposals import (
    EvidenceMapper,
    accept_questions,
    apply_coding,
    apply_seriousness,
    build_extraction,
    coding_items,
)
from pharmasafe_domain.ai.redaction import redact
from pharmasafe_domain.ai.seriousness import rule_signal
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Tracked

ORIGINAL = "Dr Maria Silva: patient JD, 64-year-old man, developed hives after Acmezol 5 mg."


def _tv(value: str, quote: str, confidence: float = 0.9) -> dict[str, Any]:
    return {"value": value, "quote": quote, "confidence": confidence}


def _extraction(**overrides: Any) -> dict[str, Any]:
    out: dict[str, Any] = {
        "patient": {"initials": _tv("JD", "patient JD"), "sex": _tv("male", "man"),
                    "age": {"value": 64, "unit": "a", "quote": "64-year-old", "confidence": 0.9},
                    "weight_kg": None},
        "reporters": [{"qualification": _tv("physician", "Dr"), "given_name": None,
                       "family_name": _tv("[NAME_1]", "Dr [NAME_1]"), "country": None}],
        "drugs": [{"product_name": _tv("Acmezol", "after Acmezol 5 mg"), "role": _tv("suspect", "after Acmezol"),
                   "dose": {"value": 5, "unit": "mg", "quote": "Acmezol 5 mg", "confidence": 0.9},
                   "route": None, "start_date": None, "indication": None}],
        "reactions": [{"reported_term": _tv("hives", "developed hives"), "onset_date": None,
                       "outcome": None}],
        "occurrence_country": None,
    }  # fmt: skip
    out.update(overrides)
    return out


def _mapper() -> EvidenceMapper:
    return EvidenceMapper(
        redact(ORIGINAL), document_id="doc-1", original=ORIGINAL, offset=100, restore=True
    )


def test_values_carry_evidence_in_original_document_offsets() -> None:
    proposals = build_extraction(SafetyCase(), _extraction(), _mapper())
    assert proposals.rejections == []
    family = proposals.case.reporters[0].family_name
    assert family is not None
    assert family.value == "Maria Silva"
    assert family.evidence is not None
    assert family.evidence.text == "Dr Maria Silva"
    assert (family.evidence.start, family.evidence.end) == (100, 114)
    drug = proposals.case.drugs[0]
    assert drug.dose is not None
    assert (str(drug.dose.value.value), drug.dose.value.unit) == ("5", "mg")
    assert proposals.case.patient.age is not None
    assert proposals.case.patient.age.value.unit.value == "a"


@pytest.mark.parametrize(
    ("override", "path", "reason"),
    [
        ({"patient": {"initials": _tv("JD", "not in the text"), "sex": None, "age": None,
                      "weight_kg": None}}, "/patient/initials", "not in the source"),
        ({"patient": {"initials": _tv("XY", "patient JD"), "sex": None, "age": None,
                      "weight_kg": None}}, "/patient/initials", "does not contain the value"),
        ({"patient": {"initials": None, "sex": _tv("robot", "man"), "age": None,
                      "weight_kg": None}}, "/patient/sex", "not a valid code"),
        ({"patient": {"initials": _tv("JD", "patient JD", 1.5), "sex": None, "age": None,
                      "weight_kg": None}}, "/patient/initials", "confidence"),
        ({"patient": {"initials": None, "sex": None, "weight_kg": None,
                      "age": {"value": 64, "unit": "lightyears", "quote": "64-year-old",
                              "confidence": 0.9}}}, "/patient/age", "age and unit"),
    ],
)  # fmt: skip
def test_values_without_valid_evidence_or_codes_are_rejected(
    override: dict[str, Any], path: str, reason: str
) -> None:
    proposals = build_extraction(SafetyCase(), _extraction(**override), _mapper())
    assert any(r.path == path and reason in r.reason for r in proposals.rejections)
    assert all(not c.path.startswith(path) for c in proposals.changes)


def test_bad_dates_and_countries_are_rejected() -> None:
    reactions = [{"reported_term": _tv("hives", "developed hives"),
                  "onset_date": _tv("yesterday", "developed hives"), "outcome": None}]  # fmt: skip
    proposals = build_extraction(
        SafetyCase(),
        _extraction(reactions=reactions, occurrence_country=_tv("United Kingdom", "Dr")),
        _mapper(),
    )
    reasons = {r.reason for r in proposals.rejections}
    assert "not a YYYY[-MM[-DD]] date" in reasons
    assert "not an ISO 3166 country code" in reasons


def test_existing_items_are_filled_not_duplicated_and_people_win() -> None:
    human = Tracked[Any].human
    now = datetime.now(UTC)
    current = SafetyCase.from_json(
        {
            "patient": {"initials": human("AB", by="p", at=now).model_dump(mode="json")},
            "drugs": [
                {
                    "id": "d1",
                    "product_name": human("ACMEZOL", by="p", at=now).model_dump(mode="json"),
                }
            ],
        }
    )
    proposals = build_extraction(current, _extraction(), _mapper())
    assert proposals.case.patient.initials is not None
    assert proposals.case.patient.initials.value == "AB"
    (drug,) = proposals.case.drugs
    assert drug.id == "d1"
    assert drug.dose is not None  # the gap was filled
    assert drug.product_name is not None
    assert drug.product_name.source.value == "human"


def test_invalid_output_is_rejected_whole() -> None:
    proposals = build_extraction(SafetyCase(), {"patient": "nonsense"}, _mapper())
    assert proposals.changes == []
    assert proposals.rejections[0].reason == "extraction output does not match the schema"


def test_seriousness_is_proposed_only_when_met_and_never_as_not_met() -> None:
    mapper = _mapper()
    proposals = build_extraction(SafetyCase(), _extraction(), mapper)
    not_met = {"met": False, "quote": None, "confidence": 0.9}
    criteria = dict.fromkeys(
        ("death", "life_threatening", "hospitalisation", "disability", "congenital_anomaly",
         "other_medically_important"), not_met,
    )  # fmt: skip
    output = {"reactions": [{"reaction_id": "ai-r1", "criteria": criteria}]}
    result = apply_seriousness(proposals, output, rule_signal(mapper.redaction.text), mapper)
    assert not result.serious
    seriousness = proposals.case.reactions[0].seriousness
    assert seriousness.death is None  # unassessed, for a person, not "False"


def test_coding_accepts_only_candidates() -> None:
    proposals = build_extraction(SafetyCase(), _extraction(), _mapper())
    items = coding_items(proposals.case, SyntheticDictionary(), 8)
    assert {i["id"] for i in items} == {"r:ai-r1", "d:ai-d1"}
    meddra = next(i for i in items if i["kind"] == "meddra_llt")
    assert "90000003" in {c["id"] for c in meddra["candidates"]}  # hives -> Urticaria
    output = {"choices": [{"item_id": "r:ai-r1", "candidate_id": "90000003", "confidence": 0.9},
                          {"item_id": "d:ai-d1", "candidate_id": "12345678", "confidence": 0.9}]}  # fmt: skip
    suggestions = apply_coding(proposals, output, items)
    assert [s["code"] for s in suggestions] == ["90000003"]
    assert proposals.case.reactions[0].meddra_code is not None
    assert proposals.case.reactions[0].meddra_code.value == "90000003"
    assert any("not one of the dictionary candidates" in r.reason for r in proposals.rejections)


def test_follow_up_questions_must_match_a_gap_and_carry_no_personal_data() -> None:
    proposals = build_extraction(SafetyCase(), _extraction(), _mapper())
    gaps = [{"path": "/drugs/ai-d1/start_date", "description": "when it started"}]
    output = {"questions": [
        {"path": "/drugs/ai-d1/start_date", "question": "When did the patient start it?"},
        {"path": "/drugs/ai-d1/start_date", "question": "Please email maria@x.example"},
        {"path": "/patient/password", "question": "What is your password?"},
    ]}  # fmt: skip
    accepted = accept_questions(proposals, output, gaps)
    assert accepted == [{"path": "/drugs/ai-d1/start_date",
                         "question": "When did the patient start it?"}]  # fmt: skip


def test_dictionary_search_is_robust_to_misspelling_and_synonyms() -> None:
    dictionary = SyntheticDictionary()
    assert dictionary.search(Kind.MEDDRA_LLT, "skin rahs", 3)[0].label == "Rash"
    assert dictionary.search(Kind.MEDDRA_LLT, "heart attack", 3)[0].label == "Myocardial infarction"
    assert dictionary.search(Kind.WHODRUG, "acetaminophen", 3)[0].code == "SYN-WD-0003"
    assert dictionary.release(Kind.WHODRUG) == "SYNTH-WHODRUG-1.0"
    assert similarity("", "rash") == 0.0
