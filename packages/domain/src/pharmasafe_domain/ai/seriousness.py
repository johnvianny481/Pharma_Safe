"""The rule signal for seriousness, and how it is combined with the LLM signal.

Two signals assess each ICH E2A criterion independently:

- the **LLM**, per reaction, with evidence;
- the **rules** here: phrases in the report text, in English and the main EU languages,
  with simple negation handling: a negation up to two words before the phrase ("no
  hospitalisation", "was not admitted to hospital"). The window is short on purpose:
  reading "no fever but was hospitalised" as negated would miss a serious case.

They are combined per criterion with OR: if either says a criterion is met, it is met.
A criterion where they disagree is **flagged** for the reviewer. Missing a serious case
costs far more than a false alarm, so a disagreement always resolves to serious, and a
model that has been talked out of seriousness (prompt injection) cannot downgrade a case
the rules see as serious.
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass

from pharmasafe_domain.rules.seriousness import CRITERIA

_PHRASES: dict[str, tuple[str, ...]] = {
    "death": (
        r"died", r"death", r"deceased", r"fatal(?:ity)?", r"passed away", r"was found dead",
        r"verstorben", r"gestorben", r"tod", r"décédée?", r"décès", r"mort", r"falleci(?:ó|da|do)",
        r"muerte", r"fallecimiento", r"decedut[oa]", r"morto", r"morta", r"decesso",
    ),
    "life_threatening": (
        r"life[- ]threatening", r"lebensbedrohlich", r"engageant le pronostic vital",
        r"potentiellement mortel(?:le)?", r"potencialmente mortal", r"pericolo di vita",
        r"risk of death", r"near[- ]fatal",
    ),
    "hospitalisation": (
        r"hospitali[sz](?:ed|ation)",
        r"admitted(?: to)?(?: the)? (?:hospital|icu|intensive care|ward)",
        r"admission to (?:hospital|icu|intensive care)", r"inpatient",
        r"prolonged hospitali[sz]ation",
        r"ins krankenhaus", r"stationär aufgenommen", r"krankenhausaufenthalt", r"hospitalisée?",
        r"hospitalizad[oa]", r"ingreso hospitalario", r"ricoverat[oa]", r"ricovero",
    ),
    "disability": (
        r"permanent(?:ly)? disab(?:led|ility)", r"persistent disability", r"significant disability",
        r"incapacity", r"bleibende behinderung", r"invalidité", r"incapacidad permanente",
        r"invalidità",
    ),
    "congenital_anomaly": (
        r"congenital anomal(?:y|ies)", r"birth defects?", r"congenital malformation",
        r"angeborene fehlbildung", r"anomalie congénitale", r"anomalía congénita",
        r"anomalia congenita",
    ),
    "other_medically_important": (
        r"anaphyla(?:xis|ctic)", r"seizures?", r"convulsions?", r"stevens[- ]johnson",
        r"toxic epidermal necrolysis", r"agranulocytosis", r"liver failure",
        r"acute (?:liver|hepatic) (?:failure|injury)", r"suicid(?:e|al)", r"torsades? de pointes",
        r"medically important",
    ),
}  # fmt: skip

_NEGATION = re.compile(
    r"(?:\bno\b|\bnot\b|\bnever\b|\bwithout\b|\bdenied\b|\bdenies\b|\bneither\b|\bnor\b"
    r"|\bdid not\b|\bwas not\b|\bwere not\b|\bkein(?:e|en)?\b|\bnicht\b|\bpas\b|\bsans\b"
    r"|\bsin\b|\bnon\b|\bsenza\b)(?:\s+[\w-]+){0,2}\s*$",
    re.IGNORECASE,
)
_COMPILED = {
    criterion: re.compile(r"(?<!\w)(?:" + "|".join(phrases) + r")(?!\w)", re.IGNORECASE)
    for criterion, phrases in _PHRASES.items()
}


@dataclass(frozen=True)
class RuleHit:
    criterion: str
    start: int
    end: int
    text: str


def rule_signal(text: str) -> dict[str, RuleHit | None]:
    """Per criterion: the first affirmative mention in the text, or None."""
    result: dict[str, RuleHit | None] = {}
    for criterion in CRITERIA:
        result[criterion] = None
        for match in _COMPILED[criterion].finditer(text):
            sentence_start = max(
                text.rfind(".", 0, match.start()), text.rfind("\n", 0, match.start())
            )
            if _NEGATION.search(text[sentence_start + 1 : match.start()]):
                continue
            result[criterion] = RuleHit(criterion, match.start(), match.end(), match.group())
            break
    return result


@dataclass(frozen=True)
class Combined:
    criterion: str
    met: bool
    llm: bool
    rules: bool

    @property
    def disagreement(self) -> bool:
        return self.llm != self.rules


def combine(llm: Mapping[str, bool], rules: Mapping[str, RuleHit | None]) -> list[Combined]:
    """Per criterion, met if either signal says so; ``disagreement`` marks a flag."""
    out = []
    for criterion in CRITERIA:
        by_llm = bool(llm.get(criterion, False))
        by_rules = rules.get(criterion) is not None
        out.append(Combined(criterion, by_llm or by_rules, by_llm, by_rules))
    return out
