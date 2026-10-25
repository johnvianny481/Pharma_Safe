"""Seriousness by the ICH E2A criteria, per reaction and per case.

This is the rule side of the dual-signal design: it reads the recorded criteria. The
LLM signal and the "disagreement means serious" rule are added in Phase 5.
"""

from pharmasafe_domain.case import Expectedness, Reaction, SafetyCase, Seriousness
from pharmasafe_domain.provenance import Tracked

CRITERIA = (
    "death",
    "life_threatening",
    "hospitalisation",
    "disability",
    "congenital_anomaly",
    "other_medically_important",
)


def met_criteria(seriousness: Seriousness) -> tuple[str, ...]:
    return tuple(name for name in CRITERIA if _is_true(getattr(seriousness, name)))


def unassessed_criteria(seriousness: Seriousness) -> tuple[str, ...]:
    return tuple(name for name in CRITERIA if getattr(seriousness, name) is None)


def reaction_is_serious(reaction: Reaction) -> bool:
    return bool(met_criteria(reaction.seriousness))


def reaction_is_fatal_or_life_threatening(reaction: Reaction) -> bool:
    s = reaction.seriousness
    return _is_true(s.death) or _is_true(s.life_threatening)


def reaction_is_unexpected(reaction: Reaction) -> bool:
    """Unexpected unless explicitly assessed as expected (conservative for reporting)."""
    e = reaction.expectedness
    return e is None or e.value is not Expectedness.EXPECTED


def case_is_serious(case: SafetyCase) -> bool:
    return any(reaction_is_serious(r) for r in case.reactions)


def _is_true(field: Tracked[bool] | None) -> bool:
    return field is not None and field.value
