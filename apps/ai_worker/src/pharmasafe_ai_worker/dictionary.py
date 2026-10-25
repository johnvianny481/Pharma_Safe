"""Dictionary adapter: coding is retrieval, never generation.

The pipeline asks the adapter for candidate terms (MedDRA lowest level terms for
reactions, WHODrug entries for drugs), and the model may only pick one of them, by id.
An id that is not among the candidates is rejected, so the model can never invent a code.

Customers license MedDRA and WHODrug and load their own releases; several releases can
be held side by side (re-coding on a version change is a later job). Development and CI
use :class:`SyntheticDictionary`, a small file of invented codes clearly marked as not
MedDRA or WHODrug content.
"""

import json
import re
import unicodedata
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

SYNTHETIC_FILE = Path(__file__).parent / "data" / "synthetic_dictionary.json"


class Kind(StrEnum):
    MEDDRA_LLT = "meddra_llt"
    WHODRUG = "whodrug"


@dataclass(frozen=True)
class Entry:
    kind: Kind
    code: str
    label: str
    synonyms: tuple[str, ...]


@dataclass(frozen=True)
class Candidate:
    code: str
    label: str
    score: float


class Dictionary(Protocol):
    def release(self, kind: Kind) -> str: ...

    def search(self, kind: Kind, term: str, limit: int) -> list[Candidate]: ...


def _norm(text: str) -> str:
    folded = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().casefold()
    return " ".join(re.findall(r"[a-z0-9]+", folded))


def _trigrams(text: str) -> set[str]:
    padded = f"  {text} "
    return {padded[i : i + 3] for i in range(len(padded) - 2)}


def similarity(a: str, b: str) -> float:
    """Blend of word overlap and character-trigram overlap (robust to misspellings)."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    words_a, words_b = set(na.split()), set(nb.split())
    word = len(words_a & words_b) / len(words_a | words_b)
    ta, tb = _trigrams(na), _trigrams(nb)
    trigram = len(ta & tb) / len(ta | tb)
    return round(0.5 * word + 0.5 * trigram, 4)


class SyntheticDictionary:
    def __init__(self, path: Path = SYNTHETIC_FILE) -> None:
        data = json.loads(path.read_text(encoding="utf-8"))
        self._releases: dict[Kind, str] = {Kind(k): str(v) for k, v in data["releases"].items()}
        self._entries = [
            Entry(Kind(e["kind"]), e["code"], e["label"], tuple(e["synonyms"]))
            for e in data["entries"]
        ]

    def release(self, kind: Kind) -> str:
        return self._releases[kind]

    def search(self, kind: Kind, term: str, limit: int) -> list[Candidate]:
        scored = []
        for entry in self._entries:
            if entry.kind is not kind:
                continue
            best = max(similarity(term, name) for name in (entry.label, *entry.synonyms))
            if best > 0.15:
                scored.append(Candidate(entry.code, entry.label, best))
        scored.sort(key=lambda c: (-c.score, c.code))
        return scored[:limit]
