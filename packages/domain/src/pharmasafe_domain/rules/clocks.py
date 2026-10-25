"""Regulatory clocks: day 0 and due dates from versioned, configurable rule sets.

**Day 0** is the date the company first had all four minimum criteria: the receipt date
(C.1.5) of the earliest known state of the case that was valid.

**Rule sets** are data, not code. Each regulator (FDA, EMA, CDSCO, ...) has rule sets
with a version and an effective date; the one in force on the clock-start date applies.
The seeded rule sets in ``rule_sets/`` are examples and say so (``"example": true``).
Each customer's regulatory affairs team must review and configure them.

**Clocks never reset.** A clock starts the first time a rule applies to a case version,
at that state's receipt date, and is never moved afterwards. Later information can only
start new clocks: for example, a follow-up that makes a case serious starts the serious
clock on the follow-up's receipt date. :class:`ClockTracker` applies this incrementally
as the case changes; :func:`evaluate_history` gives the same answer for a whole history.

Where the case does not say enough to decide (report type or country unknown, or
expectedness not assessed), the rule engine assumes the case is reportable: a missed
deadline is worse than an unnecessary report.
"""

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from enum import StrEnum
from importlib import resources
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pharmasafe_domain.case import Reaction, ReportType, SafetyCase
from pharmasafe_domain.rules.seriousness import (
    case_is_serious,
    reaction_is_fatal_or_life_threatening,
    reaction_is_serious,
    reaction_is_unexpected,
)
from pharmasafe_domain.rules.validity import assess_validity


class Setting(StrEnum):
    POSTMARKETING = "postmarketing"
    STUDY = "study"
    ANY = "any"


class SeriousnessCondition(StrEnum):
    SERIOUS = "serious"
    NON_SERIOUS = "non_serious"
    ANY = "any"


class Occurrence(StrEnum):
    DOMESTIC = "domestic"
    FOREIGN = "foreign"
    ANY = "any"


class ClockRule(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9-]{1,64}$")
    description: str
    setting: Setting = Setting.ANY
    seriousness: SeriousnessCondition = SeriousnessCondition.ANY
    unexpected_only: bool = False
    fatal_or_life_threatening_only: bool = False
    occurrence: Occurrence = Occurrence.ANY
    due_in_days: int = Field(ge=1, le=365)


class ClockRuleSet(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    regulator: str = Field(pattern=r"^[A-Z]{2,10}$")
    version: str = Field(min_length=1, max_length=40)
    effective_from: date
    example: bool
    notice: str = ""
    domestic_countries: frozenset[str]
    rules: tuple[ClockRule, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _rule_ids_are_unique(self) -> Self:
        ids = [rule.id for rule in self.rules]
        if len(ids) != len(set(ids)):
            raise ValueError(f"{self.regulator} {self.version}: rule ids must be unique")
        return self


class RuleSetRegistry:
    def __init__(self, rule_sets: Iterable[ClockRuleSet]) -> None:
        by_regulator: dict[str, list[ClockRuleSet]] = {}
        for rule_set in rule_sets:
            by_regulator.setdefault(rule_set.regulator, []).append(rule_set)
        for regulator, sets in by_regulator.items():
            sets.sort(key=lambda s: s.effective_from)
            dates = [s.effective_from for s in sets]
            if len(dates) != len(set(dates)):
                raise ValueError(f"{regulator}: two rule sets take effect on the same date")
        self._by_regulator = by_regulator

    @property
    def regulators(self) -> frozenset[str]:
        return frozenset(self._by_regulator)

    def in_force(self, regulator: str, on: date) -> ClockRuleSet:
        candidates = [s for s in self._by_regulator.get(regulator, []) if s.effective_from <= on]
        if not candidates:
            raise LookupError(f"no {regulator} rule set in force on {on.isoformat()}")
        return candidates[-1]


def load_rule_set(text: str) -> ClockRuleSet:
    return ClockRuleSet.model_validate(json.loads(text))


def seed_registry() -> RuleSetRegistry:
    """The example rule sets shipped with PharmaSafe (``rule_sets/*.json``)."""
    folder = resources.files("pharmasafe_domain.rules") / "rule_sets"
    return RuleSetRegistry(
        load_rule_set(entry.read_text(encoding="utf-8"))
        for entry in sorted(folder.iterdir(), key=lambda e: e.name)
        if entry.name.endswith(".json")
    )


# ---------------------------------------------------------------- applying rules


def rule_applies(rule: ClockRule, rule_set: ClockRuleSet, case: SafetyCase) -> bool:
    """Whether a valid case falls under this rule. Validity is checked by the caller."""
    return (
        _setting_matches(rule.setting, case)
        and _occurrence_matches(rule.occurrence, rule_set, case)
        and _reactions_match(rule, case)
    )


def _setting_matches(setting: Setting, case: SafetyCase) -> bool:
    if setting is Setting.ANY or case.admin.report_type is None:
        return True
    is_study = case.admin.report_type.value is ReportType.STUDY
    return is_study == (setting is Setting.STUDY)


def _occurrence_matches(occurrence: Occurrence, rule_set: ClockRuleSet, case: SafetyCase) -> bool:
    country = case.admin.occurrence_country
    if occurrence is Occurrence.ANY or country is None:
        return True
    domestic = country.value in rule_set.domestic_countries
    return domestic == (occurrence is Occurrence.DOMESTIC)


def _reactions_match(rule: ClockRule, case: SafetyCase) -> bool:
    if rule.seriousness is SeriousnessCondition.NON_SERIOUS and case_is_serious(case):
        return False

    def qualifies(reaction: Reaction) -> bool:
        if rule.seriousness is SeriousnessCondition.SERIOUS and not reaction_is_serious(reaction):
            return False
        if rule.unexpected_only and not reaction_is_unexpected(reaction):
            return False
        return not (
            rule.fatal_or_life_threatening_only
            and not reaction_is_fatal_or_life_threatening(reaction)
        )

    return any(qualifies(r) for r in case.reactions)


# ---------------------------------------------------------------- clocks over time


@dataclass(frozen=True)
class Obligation:
    """One report owed to one regulator for one case version, and when it is due."""

    regulator: str
    rule_id: str
    rule_set_version: str
    case_version: int
    clock_start: date
    due_date: date

    @property
    def key(self) -> tuple[str, str, int]:
        return (self.regulator, self.rule_id, self.case_version)


@dataclass(frozen=True)
class Snapshot:
    """The case as it was known at one moment, within one case version."""

    case_version: int
    case: SafetyCase


def day_zero(history: Iterable[Snapshot]) -> date | None:
    for snapshot in history:
        receipt = snapshot.case.admin.latest_receipt_date
        if receipt is not None and assess_validity(snapshot.case).valid:
            return receipt.value
    return None


class ClockTracker:
    """Starts clocks as a case changes; never moves a clock that has started."""

    def __init__(
        self,
        registry: RuleSetRegistry,
        regulators: Iterable[str],
        started: Iterable[Obligation] = (),
    ) -> None:
        self._registry = registry
        self._regulators = tuple(sorted(set(regulators)))
        self._started: dict[tuple[str, str, int], Obligation] = {o.key: o for o in started}

    @property
    def obligations(self) -> Sequence[Obligation]:
        return sorted(self._started.values(), key=lambda o: (o.due_date, o.key))

    def observe(self, snapshot: Snapshot) -> list[Obligation]:
        """Record a new state of the case. Returns the clocks it started."""
        case = snapshot.case
        receipt = case.admin.latest_receipt_date
        if receipt is None or not assess_validity(case).valid:
            return []
        start = receipt.value
        new: list[Obligation] = []
        for regulator in self._regulators:
            rule_set = self._registry.in_force(regulator, start)
            for rule in rule_set.rules:
                key = (regulator, rule.id, snapshot.case_version)
                if key in self._started or not rule_applies(rule, rule_set, case):
                    continue
                obligation = Obligation(
                    regulator=regulator,
                    rule_id=rule.id,
                    rule_set_version=rule_set.version,
                    case_version=snapshot.case_version,
                    clock_start=start,
                    due_date=start + timedelta(days=rule.due_in_days),
                )
                self._started[key] = obligation
                new.append(obligation)
        return new


def evaluate_history(
    history: Iterable[Snapshot], registry: RuleSetRegistry, regulators: Iterable[str]
) -> Sequence[Obligation]:
    tracker = ClockTracker(registry, regulators)
    for snapshot in history:
        tracker.observe(snapshot)
    return tracker.obligations
