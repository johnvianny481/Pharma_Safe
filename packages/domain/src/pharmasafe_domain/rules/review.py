"""Human review: who must sign a case version, in what order, and the four-eyes rule.

Each review step is an e-signature bound to the version's content hash:

1. **Data entry review** (``Reviewed``): a case processor confirms the data.
2. **Quality review** (``Quality reviewed``): for every serious case and for a sampled
   share of non-serious cases.
3. **Medical review** (``Medically approved``): for every serious case.
4. **Approval** (``Approved``) locks the version.

Only signatures on the *current* content count, so an edit after a review voids that
review automatically. **Four eyes:** nobody who signed the data entry review may sign a
later step of the same version.

These rules are checked from the signature records themselves, independent of the
workflow engine. The workflow decides which task to open next. This module decides
whether a signature may be applied, so a missed or reordered workflow step can never
approve a case.
"""

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum

from pharmasafe_domain.case import SafetyCase
from pharmasafe_domain.rules.checks import Issue
from pharmasafe_domain.rules.seriousness import case_is_serious


class Step(StrEnum):
    """Signature meanings, as stored on e-signatures."""

    DATA_ENTRY = "Reviewed"
    QUALITY = "Quality reviewed"
    MEDICAL = "Medically approved"
    APPROVAL = "Approved"


@dataclass(frozen=True)
class SignatureFact:
    meaning: str
    signer_id: str
    content_hash: str


@dataclass(frozen=True)
class ReviewPlan:
    quality: bool
    medical: bool

    @property
    def steps(self) -> tuple[Step, ...]:
        middle = (Step.QUALITY,) * self.quality + (Step.MEDICAL,) * self.medical
        return (Step.DATA_ENTRY, *middle, Step.APPROVAL)


def qc_sampled(case_id: str, version: int, rate: Decimal) -> bool:
    """Deterministic sampling for quality control of non-serious cases.

    The same case version is always in or out of the sample, whichever process asks.
    """
    digest = hashlib.sha256(f"{case_id}:{version}".encode()).digest()
    position = Decimal(int.from_bytes(digest[:8], "big")) / Decimal(2**64)
    return position < rate


def review_plan(case: SafetyCase, *, sampled: bool, flagged_serious: bool = False) -> ReviewPlan:
    """Serious cases (recorded criteria, or flagged by the AI seriousness signals) need
    quality and medical review; sampled non-serious cases need quality review."""
    serious = flagged_serious or case_is_serious(case)
    return ReviewPlan(quality=serious or sampled, medical=serious)


def signing_blockers(
    step: Step,
    signer_id: str,
    signatures: Iterable[SignatureFact],
    plan: ReviewPlan,
    content_hash: str,
) -> list[Issue]:
    """Why ``signer_id`` may not sign ``step`` on this content now. Empty means they may."""
    current = [s for s in signatures if s.content_hash == content_hash]
    data_entry = {s.signer_id for s in current if s.meaning == Step.DATA_ENTRY}
    if step is Step.DATA_ENTRY:
        return []

    blockers = []
    if not data_entry:
        blockers.append(Issue("review_missing", "/", "the data entry review must be signed first"))
    if signer_id in data_entry:
        blockers.append(
            Issue("four_eyes", "/", "a second person must sign after the data entry reviewer")
        )
    if step is Step.APPROVAL:
        for needed, required in ((Step.QUALITY, plan.quality), (Step.MEDICAL, plan.medical)):
            others = {s.signer_id for s in current if s.meaning == needed} - data_entry
            if required and not others:
                code = (
                    "quality_review_missing" if needed is Step.QUALITY else "medical_review_missing"
                )
                blockers.append(
                    Issue(code, "/", f"'{needed.value}' must be signed before approval")
                )
    return blockers
