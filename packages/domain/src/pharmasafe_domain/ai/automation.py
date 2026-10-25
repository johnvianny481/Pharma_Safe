"""Automation levels: how far AI output goes before a person acts.

| Level | AI does | People do |
|---|---|---|
| L0 assist | suggests values in a side panel; nothing written to the case | enter all data |
| L1 pre-fill | fills the form with values and evidence, unconfirmed | confirm each field, sign |
| L2 straight-through | as L1, and marks the case for bulk QC sign-off | sample and sign in bulk |

Rules (in this order):

1. If the AI is unavailable (provider outage, budget exhausted), the case is processed in
   **L0 manual mode**, whatever the tenant's setting.
2. **Serious cases never go beyond L1**, whichever signal said serious.
3. L2 also needs a valid case and every AI value at or above the confidence threshold.

L2 never skips a signature: approval still requires every value to be confirmed, and the
bulk sign-off screen is part of the review UI (Phase 6).
"""

from dataclasses import dataclass
from enum import StrEnum


class Level(StrEnum):
    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


@dataclass(frozen=True)
class Decision:
    level: Level
    reasons: tuple[str, ...]


def effective_level(
    tenant_level: Level,
    *,
    ai_available: bool,
    serious: bool,
    valid: bool,
    min_confidence: float | None,
    threshold: float,
) -> Decision:
    if not ai_available:
        return Decision(Level.L0, ("AI unavailable: manual mode",))
    if tenant_level is Level.L0:
        return Decision(Level.L0, ("tenant setting",))
    if tenant_level is Level.L1:
        return Decision(Level.L1, ("tenant setting",))
    reasons = []
    if serious:
        reasons.append("serious cases are capped at L1")
    if not valid:
        reasons.append("the case is not valid")
    if min_confidence is None or min_confidence < threshold:
        reasons.append(f"an AI value is below the confidence threshold {threshold}")
    if reasons:
        return Decision(Level.L1, tuple(reasons))
    return Decision(Level.L2, ("non-serious, valid and high-confidence",))
