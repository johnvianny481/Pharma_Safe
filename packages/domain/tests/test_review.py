from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.provenance import Tracked
from pharmasafe_domain.rules.review import (
    ReviewPlan,
    SignatureFact,
    Step,
    qc_sampled,
    review_plan,
    signing_blockers,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)
HASH = "a" * 64
OLD = "b" * 64
PLAIN = ReviewPlan(quality=False, medical=False)
SERIOUS = ReviewPlan(quality=True, medical=True)


def sig(meaning: Step, signer: str, content_hash: str = HASH) -> SignatureFact:
    return SignatureFact(meaning.value, signer, content_hash)


def codes(step: Step, signer: str, sigs: list[SignatureFact], plan: ReviewPlan = PLAIN) -> set[str]:
    return {i.code for i in signing_blockers(step, signer, sigs, plan, HASH)}


def test_data_entry_review_can_always_be_signed() -> None:
    assert codes(Step.DATA_ENTRY, "proc-1", []) == set()


def test_later_steps_need_the_data_entry_review_first() -> None:
    for step in (Step.QUALITY, Step.MEDICAL, Step.APPROVAL):
        assert "review_missing" in codes(step, "qa-1", [])


def test_four_eyes_the_data_entry_reviewer_cannot_sign_later_steps() -> None:
    reviewed = [sig(Step.DATA_ENTRY, "proc-1")]
    for step in (Step.QUALITY, Step.MEDICAL, Step.APPROVAL):
        assert codes(step, "proc-1", reviewed) == {"four_eyes"}
        assert codes(step, "qa-1", reviewed) == set()


def test_serious_case_approval_needs_quality_and_medical_review_by_others() -> None:
    reviewed = [sig(Step.DATA_ENTRY, "proc-1")]
    assert codes(Step.APPROVAL, "qa-1", reviewed, SERIOUS) == {
        "quality_review_missing",
        "medical_review_missing",
    }
    # A quality review by the data entry reviewer does not count.
    with_self_review = [*reviewed, sig(Step.QUALITY, "proc-1"), sig(Step.MEDICAL, "doc-1")]
    assert codes(Step.APPROVAL, "qa-1", with_self_review, SERIOUS) == {"quality_review_missing"}
    complete = [*reviewed, sig(Step.QUALITY, "qa-2"), sig(Step.MEDICAL, "doc-1")]
    assert codes(Step.APPROVAL, "qa-1", complete, SERIOUS) == set()


def test_signatures_on_earlier_content_do_not_count() -> None:
    stale = [sig(Step.DATA_ENTRY, "proc-1", OLD)]
    assert "review_missing" in codes(Step.APPROVAL, "qa-1", stale)


def test_plan_follows_seriousness_and_sampling() -> None:
    def hv(value: Any) -> dict[str, Any]:
        return Tracked[Any].human(value, by="u", at=T0).model_dump(mode="json")

    serious = SafetyCase.from_json(
        {"reactions": [{"id": "r1", "seriousness": {"death": hv(True)}}]}
    )
    plain = SafetyCase.from_json({"reactions": [{"id": "r1", "seriousness": {"death": hv(False)}}]})
    assert review_plan(serious, sampled=False) == SERIOUS
    assert review_plan(plain, sampled=False) == PLAIN
    assert review_plan(plain, sampled=True) == ReviewPlan(quality=True, medical=False)
    assert SERIOUS.steps == (Step.DATA_ENTRY, Step.QUALITY, Step.MEDICAL, Step.APPROVAL)
    assert PLAIN.steps == (Step.DATA_ENTRY, Step.APPROVAL)


@given(st.uuids(), st.integers(1, 20))
def test_sampling_is_deterministic_and_respects_the_bounds(case_id: Any, version: int) -> None:
    assert qc_sampled(str(case_id), version, Decimal(1))
    assert not qc_sampled(str(case_id), version, Decimal(0))
    rate = Decimal("0.3")
    assert qc_sampled(str(case_id), version, rate) == qc_sampled(str(case_id), version, rate)


def test_sampling_rate_is_roughly_honoured() -> None:
    sampled = sum(qc_sampled(f"case-{n}", 1, Decimal("0.1")) for n in range(5000))
    assert 400 < sampled < 600


@pytest.mark.parametrize("step", list(Step))
def test_every_step_has_a_stored_meaning(step: Step) -> None:
    assert step.value in {"Reviewed", "Quality reviewed", "Medically approved", "Approved"}
