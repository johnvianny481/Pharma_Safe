"""Evidence checks, the rule seriousness signal, duplicates, automation levels, gaps."""

from datetime import UTC, datetime
from typing import Any

import pytest

from pharmasafe_domain.ai.automation import Level, effective_level
from pharmasafe_domain.ai.duplicates import (
    Candidate,
    Keys,
    blocking_keys,
    cosine,
    score,
    top_candidates,
    vectorize,
)
from pharmasafe_domain.ai.evidence import Rejection, check_evidence, locate, supports
from pharmasafe_domain.ai.followup import missing_information
from pharmasafe_domain.ai.seriousness import combine, rule_signal
from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.review import review_plan

TEXT = "A 64-year-old man developed a severe rash two days after starting Acmezol 20 mg."


# ---------------------------------------------------------------- evidence


def test_evidence_must_be_a_verbatim_quote() -> None:
    assert locate("severe rash", TEXT) is not None
    assert isinstance(check_evidence("/x", "severe Rash", TEXT), Rejection)
    assert isinstance(check_evidence("/x", None, TEXT), Rejection)
    assert isinstance(check_evidence("/x", "  ", TEXT), Rejection)
    assert isinstance(check_evidence("/x", "x" * 501, "x" * 600), Rejection)


def test_free_text_values_must_be_in_their_quote() -> None:
    assert supports("Acmezol", "after starting Acmezol 20 mg")
    assert not supports("Evilzol", "after starting Acmezol 20 mg")
    rejected = check_evidence("/drugs/d1/product_name", "a severe rash", TEXT, value="Acmezol")
    assert isinstance(rejected, Rejection)
    assert rejected.reason == "the quoted evidence does not contain the value"
    found = check_evidence("/drugs/d1/product_name", "starting Acmezol", TEXT, value="acmezol")
    assert not isinstance(found, Rejection)
    assert TEXT[found.start : found.end] == "starting Acmezol"


def test_locate_prefers_the_occurrence_at_or_after_a_position() -> None:
    text = "rash ... rash"
    assert locate("rash", text, near=3) is not None
    located = locate("rash", text, near=3)
    assert located is not None
    assert located.start == 9


# ---------------------------------------------------------------- seriousness signals


@pytest.mark.parametrize(
    ("text", "criterion"),
    [
        ("The patient died on day 3.", "death"),
        ("This was a life-threatening reaction.", "life_threatening"),
        ("She had no fever but was hospitalised.", "hospitalisation"),
        ("He was admitted to the ICU.", "hospitalisation"),
        ("Er wurde ins Krankenhaus eingeliefert.", "hospitalisation"),
        ("È stata ricoverata.", "hospitalisation"),
        ("Permanent disability followed.", "disability"),
        ("The baby had a birth defect.", "congenital_anomaly"),
        ("Anaphylactic reaction within minutes.", "other_medically_important"),
    ],
)
def test_rule_signal_finds_criteria(text: str, criterion: str) -> None:
    hit = rule_signal(text)[criterion]
    assert hit is not None
    assert text[hit.start : hit.end] == hit.text


@pytest.mark.parametrize(
    "text",
    [
        "No hospitalisation was needed.",
        "He was not admitted to hospital.",
        "Recovered without requiring hospitalization.",
        "Kein Krankenhausaufenthalt.",
    ],
)
def test_negated_mentions_do_not_count(text: str) -> None:
    assert rule_signal(text)["hospitalisation"] is None


def test_signals_combine_with_or_and_flag_disagreement() -> None:
    rules = rule_signal("He was hospitalised.")
    combined = {c.criterion: c for c in combine({"death": True}, rules)}
    assert combined["death"].met
    assert combined["death"].disagreement
    assert combined["hospitalisation"].met
    assert combined["hospitalisation"].disagreement
    assert not combined["disability"].met
    assert not combined["disability"].disagreement


# ---------------------------------------------------------------- duplicates


def _h(value: Any) -> dict[str, Any]:
    return Tracked[Any].human(value, by="p", at=datetime.now(UTC)).model_dump(mode="json")


def _case(drug: str, role: str = "suspect", term: str = "Rash") -> SafetyCase:
    return SafetyCase.from_json(
        {
            "patient": {
                "initials": _h("JD"),
                "sex": _h("male"),
                "age": _h({"value": "64", "unit": "a"}),
            },
            "admin": {"occurrence_country": _h("US")},
            "drugs": [{"id": "d1", "role": _h(role), "product_name": _h(drug)}],
            "reactions": [{"id": "r1", "reported_term": _h(term), "onset_date": _h("2026-10-01")}],
        }
    )


def test_blocking_keys_are_normalised_and_skip_concomitant_drugs() -> None:
    keys = blocking_keys(_case("Acmézol"))
    assert keys.drugs == {"acmezol"}
    assert keys.reactions == {"rash"}
    assert keys.onsets == {"2026-10-01"}
    assert keys.initials == "jd"
    assert (keys.sex, keys.age, keys.country) == ("male", "64 a", "US")
    assert blocking_keys(_case("Acmezol", role="concomitant")).drugs == frozenset()


def test_vectors_are_deterministic_and_ignore_tokens() -> None:
    a = vectorize("[NAME_1] developed a rash after Acmezol")
    assert a == vectorize("[NAME_7] developed a rash after Acmezol")
    assert cosine(a, a) == pytest.approx(1.0)
    near = vectorize("developed a skin rash after taking Acmezol")
    far = vectorize("headache and nausea following vaccination")
    assert cosine(a, near) > cosine(a, far)
    assert vectorize("") == [0.0] * 256


def test_score_explains_itself() -> None:
    mine = blocking_keys(_case("Acmezol"))
    points, reasons = score(mine, mine, 0.9)
    assert points == pytest.approx(0.9675)
    assert "same suspect drug: acmezol" in reasons
    assert "text similarity 0.90" in reasons
    low, _ = score(mine, Keys(), 2.0)  # similarity is clamped to 1
    assert low == pytest.approx(0.325)


def test_top_candidates_keeps_the_best_above_the_threshold() -> None:
    candidates = [Candidate(str(i), f"C-{i}", s, ()) for i, s in enumerate((0.4, 0.9, 0.7, 0.8))]
    assert [c.score for c in top_candidates(candidates, top=2, min_score=0.5)] == [0.9, 0.8]


# ---------------------------------------------------------------- automation


@pytest.mark.parametrize(
    ("tenant", "kwargs", "level"),
    [
        (Level.L2, {"ai_available": False}, Level.L0),
        (Level.L0, {}, Level.L0),
        (Level.L1, {}, Level.L1),
        (Level.L2, {}, Level.L2),
        (Level.L2, {"serious": True}, Level.L1),
        (Level.L2, {"valid": False}, Level.L1),
        (Level.L2, {"min_confidence": 0.5}, Level.L1),
        (Level.L2, {"min_confidence": None}, Level.L1),
    ],
)
def test_automation_level(tenant: Level, kwargs: dict[str, Any], level: Level) -> None:
    args: dict[str, Any] = {"ai_available": True, "serious": False, "valid": True,
                            "min_confidence": 0.95, "threshold": 0.9} | kwargs  # fmt: skip
    decision = effective_level(tenant, **args)
    assert decision.level is level
    assert decision.reasons


def test_serious_cases_never_go_beyond_l1() -> None:
    decision = effective_level(
        Level.L2, ai_available=True, serious=True, valid=True, min_confidence=1.0, threshold=0.9
    )
    assert decision.level is Level.L1
    assert "serious cases are capped at L1" in decision.reasons


# ---------------------------------------------------------------- follow-up gaps


def test_gaps_cover_validity_dates_doses_outcomes_and_seriousness() -> None:
    paths = {g.path for g in missing_information(_case("Acmezol"))}
    assert paths == {
        "/reporters",
        "/reactions/r1/outcome",
        "/reactions/r1/seriousness",
        "/drugs/d1/dose",
        "/drugs/d1/start_date",
    }
    assert {g.path for g in missing_information(SafetyCase())} == {
        "/patient", "/reporters", "/drugs", "/reactions",
    }  # fmt: skip


def test_review_plan_follows_the_ai_seriousness_flag() -> None:
    plan = review_plan(SafetyCase(), sampled=False, flagged_serious=True)
    assert plan.quality
    assert plan.medical
    assert not review_plan(SafetyCase(), sampled=False).medical
