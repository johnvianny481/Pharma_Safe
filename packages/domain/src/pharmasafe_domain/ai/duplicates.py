"""Duplicate detection: blocking keys first, then text similarity, with reasons.

1. **Blocking.** Only cases sharing a suspect drug or a reaction term (normalised) are
   compared at all, which keeps the comparison cheap at 100,000 cases a month. The case
   store does this in SQL from :func:`blocking_keys`.
2. **Similarity.** Each case has a vector of hashed character n-grams of its redacted
   summary (:func:`vectorize`), stored in pgvector. It is deterministic and needs no
   model, so identical text always gives identical vectors and nothing leaves the
   region. It catches re-sent and lightly edited reports; it is weaker on paraphrase,
   which is why the keys carry most of the weight.
3. **Scoring.** :func:`score` combines matching keys and similarity into a score with a
   human-readable reason for each part. The top candidates go to the reviewer, who
   decides; nothing is merged automatically.
"""

import hashlib
import math
import re
import unicodedata
from dataclasses import dataclass, field

from pharmasafe_domain.ai.redaction import TOKEN
from pharmasafe_domain.case import DrugRole, SafetyCase

WEIGHTS = {
    "drug": 0.25,
    "reaction": 0.2,
    "onset": 0.1,
    "initials": 0.05,
    "sex": 0.025,
    "age": 0.025,
    "country": 0.025,
    "similarity": 0.325,
}


def normalise_term(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


@dataclass(frozen=True)
class Keys:
    drugs: frozenset[str] = field(default_factory=frozenset)
    reactions: frozenset[str] = field(default_factory=frozenset)
    onsets: frozenset[str] = field(default_factory=frozenset)
    initials: str | None = None
    sex: str | None = None
    age: str | None = None
    country: str | None = None


def blocking_keys(case: SafetyCase) -> Keys:
    drugs = set()
    for drug in case.drugs:
        if drug.role is not None and drug.role.value is DrugRole.CONCOMITANT:
            continue
        for name in (drug.product_name, drug.active_substance):
            if name is not None:
                drugs.add(normalise_term(name.value))
    reactions = {normalise_term(r.reported_term.value) for r in case.reactions if r.reported_term}
    onsets = {str(r.onset_date.value) for r in case.reactions if r.onset_date}
    p = case.patient
    return Keys(
        drugs=frozenset(d for d in drugs if d),
        reactions=frozenset(r for r in reactions if r),
        onsets=frozenset(onsets),
        initials=normalise_term(p.initials.value) if p.initials else None,
        sex=p.sex.value.value if p.sex else None,
        age=f"{p.age.value.value} {p.age.value.unit.value}" if p.age else None,
        country=case.admin.occurrence_country.value if case.admin.occurrence_country else None,
    )


def vectorize(text: str, *, n: int = 3, dimensions: int = 256) -> list[float]:
    """L2-normalised vector of hashed character n-grams (redaction tokens removed)."""
    cleaned = " ".join(re.findall(r"\w+", TOKEN.sub(" ", text).casefold()))
    vector = [0.0] * dimensions
    padded = f" {cleaned} "
    for i in range(max(len(padded) - n + 1, 0)):
        gram = padded[i : i + n].encode()
        digest = hashlib.blake2b(gram, digest_size=8).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimensions
        sign = 1.0 if digest[4] & 1 else -1.0
        vector[bucket] += sign
    norm = math.sqrt(sum(v * v for v in vector))
    return [v / norm for v in vector] if norm else vector


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


@dataclass(frozen=True)
class Candidate:
    case_id: str
    case_number: str
    score: float
    reasons: tuple[str, ...]


def score(new: Keys, other: Keys, similarity: float) -> tuple[float, tuple[str, ...]]:
    """Score in [0, 1] and the reasons behind it."""
    total = 0.0
    reasons: list[str] = []

    def shared(name: str, mine: frozenset[str], theirs: frozenset[str], label: str) -> None:
        nonlocal total
        common = sorted(mine & theirs)
        if common:
            total += WEIGHTS[name]
            reasons.append(f"same {label}: {', '.join(common)}")

    def same(name: str, mine: str | None, theirs: str | None, label: str) -> None:
        nonlocal total
        if mine is not None and mine == theirs:
            total += WEIGHTS[name]
            reasons.append(f"same {label}: {mine}")

    shared("drug", new.drugs, other.drugs, "suspect drug")
    shared("reaction", new.reactions, other.reactions, "reaction")
    shared("onset", new.onsets, other.onsets, "onset date")
    same("initials", new.initials, other.initials, "patient initials")
    same("sex", new.sex, other.sex, "patient sex")
    same("age", new.age, other.age, "patient age")
    same("country", new.country, other.country, "country")
    clamped = max(0.0, min(1.0, similarity))
    total += WEIGHTS["similarity"] * clamped
    reasons.append(f"text similarity {clamped:.2f}")
    return round(total, 4), tuple(reasons)


def top_candidates(candidates: list[Candidate], *, top: int, min_score: float) -> list[Candidate]:
    kept = [c for c in candidates if c.score >= min_score]
    return sorted(kept, key=lambda c: (-c.score, c.case_number))[:top]
