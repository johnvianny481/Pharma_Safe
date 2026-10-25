"""Personal-data redaction before any model call: reversible tokens.

Direct identifiers are replaced with tokens such as ``[NAME_1]`` or ``[EMAIL_1]``. The
same original value always gets the same token within one text, so the model can still
tell that two mentions are the same person. The mapping from token to original is kept
by the caller, encrypted (see ``pharmasafe_platform.encryption``); only the case service
can reverse it.

**What is detected** (rules, no model):

- email addresses, telephone numbers, URLs;
- ID numbers with a label (MRN, patient ID, NHS, SSN, passport, insurance, ...);
- dates of birth with a label (DOB, born, date of birth);
- street addresses (house number and a street word) and postcodes after a city label;
- names after a cue: titles (Dr, Mr, Mrs, Ms, Prof, ...), roles (patient, reporter,
  pharmacist, ...), and labels (``Name:``), in English and the main EU languages;
- any value the caller already knows to be personal (for example names entered on the
  intake form), wherever it appears.

**Limits.** A name written with no cue ("Maria called on Monday") is not found. The
gateway re-runs these rules on every outgoing request as a second check, and a
model-based detector can be added behind :func:`find_identifiers` later. Patient
initials, ages and sex are not direct identifiers and are kept: they are needed for case
validity and duplicate detection.

Offsets: :meth:`Redaction.to_original` maps a span of the redacted text back to the
original text, so evidence quoted by the model points at the real source document.
"""

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

TOKEN = re.compile(r"\[(?:NAME|EMAIL|PHONE|ID|DOB|ADDRESS|URL)_\d+\]")


class Kind(StrEnum):
    NAME = "NAME"
    EMAIL = "EMAIL"
    PHONE = "PHONE"
    ID = "ID"
    DOB = "DOB"
    ADDRESS = "ADDRESS"
    URL = "URL"


@dataclass(frozen=True)
class Found:
    start: int
    end: int
    kind: Kind


# A capitalised word with at least one lower-case letter (so initials such as "JD" and
# acronyms such as "ICU" are not names). Allows O'Brien, McDonald and Müller-Lüdenscheid.
_APOSTROPHES = "'" + chr(0x2019)  # straight and typographic
_NAME_WORD = (
    rf"(?=[A-ZÀ-ÖØ-Þ][\w{_APOSTROPHES}-]*[a-zà-öø-ÿ])[A-ZÀ-ÖØ-Þ](?:[{_APOSTROPHES}]?[A-Za-zÀ-ÖØ-öø-ÿ])+"
    r"(?:-[A-ZÀ-ÖØ-Þ][a-zà-öø-ÿ]+)?"
)
_NAME = rf"{_NAME_WORD}(?:[ \t]+(?:(?:van|von|de|da|di|del|der|le|la)[ \t]+)?{_NAME_WORD}){{0,3}}"
_TITLES = (
    r"Dr|Dr\.|Doctor|Mr|Mr\.|Mrs|Mrs\.|Ms|Ms\.|Miss|Prof|Prof\.|Professor|Nurse|Sister"
    r"|Herr|Frau|Monsieur|Madame|Mme|M\.|Señor|Señora|Sr\.|Sra\.|Signor|Signora|Sig\.|Sig\.ra"
)
_ROLES = (
    r"patient|reporter|pharmacist|physician|nurse|caller|consumer|mother|father|wife|husband"
    r"|daughter|son|Patient|Patientin|patiente|paciente|paziente|Reporter|Melder|Melderin"
)
_LABELS = r"name|full name|patient name|reporter name|signed|contact|Name|Nom|Nombre|Nome"

_PATTERNS: tuple[tuple[Kind, re.Pattern[str], int], ...] = (
    (Kind.EMAIL, re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}"), 0),
    (Kind.URL, re.compile(r"\bhttps?://[^\s<>\"]+", re.IGNORECASE), 0),
    (
        Kind.ID,
        re.compile(
            r"(?i:\b(?:MRN|medical record(?: number| no\.?)?|patient (?:id|number|no\.?)|hospital"
            r" (?:number|no\.?)|NHS(?: number| no\.?)?|SSN|social security(?: number)?|passport"
            r"(?: number| no\.?)?|insurance (?:number|no\.?)|ID(?: number| no\.?)?)\s*[:#]?\s*)"
            r"([A-Z0-9][A-Z0-9 -]{3,20}[A-Z0-9])"
        ),
        1,
    ),
    (
        Kind.DOB,
        re.compile(
            r"(?i:\b(?:DOB|D\.O\.B\.|date of birth|born(?: on)?|geboren(?: am)?|né(?:e)? le"
            r"|nacid[oa] el|nat[oa] il)\s*[:]?\s*)"
            r"(\d{1,4}[./-]\d{1,2}[./-]\d{1,4}|\d{1,2}\s+[A-Za-zÀ-ÿ]{3,9}\.?\s+\d{4}"
            r"|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})"
        ),
        1,
    ),
    (
        Kind.PHONE,
        re.compile(
            r"(?<![\w.])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?"
            r"\d{2,4}(?:[ .-]\d{2,4}){2,4}(?![\w.])"
        ),
        0,
    ),
    (
        Kind.ADDRESS,
        re.compile(
            r"\b\d{1,5}[A-Za-z]?\s+(?:[A-Z][a-zà-ÿ]+\s+){1,3}"
            r"(?:Street|St\.?|Road|Rd\.?|Avenue|Ave\.?|Lane|Ln\.?|Drive|Dr\.?|Boulevard|Blvd\.?"
            r"|Way|Close|Court|Place|Square)\b"
            r"|\b(?:[A-Z][a-zà-ÿ]+)(?:straße|strasse|weg|gasse|platz)\s+\d{1,4}[a-z]?\b"
            r"|\b\d{1,4},?\s+(?:rue|avenue|boulevard|place|calle|via|viale|piazza)\s+"
            r"(?:(?:de|du|des|la|le|del|della|di)\s+)?[A-ZÀ-Ý][a-zà-ÿ]+(?:\s+[A-ZÀ-Ý][a-zà-ÿ]+)?"
        ),
        0,
    ),
    (Kind.NAME, re.compile(rf"\b(?:{_TITLES})[ \t]+({_NAME})"), 1),
    (Kind.NAME, re.compile(rf"\b(?:{_ROLES})[ \t]+({_NAME})"), 1),
    (Kind.NAME, re.compile(rf"\b(?:{_LABELS})[ \t]*[:=][ \t]*({_NAME})"), 1),
)

# Words that follow a cue but are not names ("Patient Information", "Dr Notes").
_NOT_NAMES = frozenset(
    {
        "information", "report", "reported", "notes", "summary", "history", "safety",
        "the", "a", "an", "is", "was", "had", "has", "and", "or", "with", "who", "on",
        "in", "at", "of", "for", "to", "experienced", "developed", "took", "presented",
        "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
        "january", "february", "march", "april", "may", "june", "july", "august",
        "september", "october", "november", "december",
    }
)  # fmt: skip


def find_identifiers(text: str, known: Iterable[str] = ()) -> list[Found]:
    """Spans of direct identifiers in ``text``, without overlaps, in order."""
    found: list[Found] = []
    for kind, pattern, group in _PATTERNS:
        for match in pattern.finditer(text):
            start, end = match.span(group)
            if start < 0:
                continue
            if kind is Kind.NAME:
                start, end = _trim_name(text, start, end)
                if start >= end:
                    continue
            if kind is Kind.PHONE and not _is_phone(text[start:end]):
                continue
            if _inside_token(text, start, end):
                continue
            found.append(Found(start, end, kind))
    for value in known:
        value = value.strip()
        if len(value) < 2:
            continue
        for match in re.finditer(rf"(?<!\w){re.escape(value)}(?!\w)", text, re.IGNORECASE):
            found.append(Found(match.start(), match.end(), Kind.NAME))
    return _without_overlaps(found)


_TITLE_WORDS = frozenset(t.casefold().rstrip(".") for t in _TITLES.replace("\\.", ".").split("|"))
_DATE_LIKE = re.compile(r"^\d{4}[./-]\d{1,2}[./-]\d{1,2}$|^\d{1,2}[./-]\d{1,2}[./-]\d{2,4}$")


def _is_phone(candidate: str) -> bool:
    """At least 7 digits, and not a date such as 2026-10-01 or 01.10.2026."""
    return sum(c.isdigit() for c in candidate) >= 7 and not _DATE_LIKE.match(candidate)


def _trim_name(text: str, start: int, end: int) -> tuple[int, int]:
    """Keep the name itself: "Dr Silva developed" -> "Silva".

    Titles stay in the text (the reporter's qualification is read from them), and
    trailing words that are not names are dropped.
    """
    words = list(re.finditer(r"\S+", text[start:end]))
    while words and words[0].group().casefold().rstrip(".") in _TITLE_WORDS:
        words.pop(0)
    kept = []
    for word in words:
        if word.group().casefold().strip(".,;:") in _NOT_NAMES:
            break
        kept.append(word)
    if not kept:
        return start, start
    return start + kept[0].start(), start + kept[-1].end()


def _inside_token(text: str, start: int, end: int) -> bool:
    return any(m.start() <= start and end <= m.end() for m in TOKEN.finditer(text))


def _without_overlaps(found: list[Found]) -> list[Found]:
    # Longest first at each start, then drop anything overlapping an earlier keeper.
    ordered = sorted(found, key=lambda f: (f.start, -(f.end - f.start)))
    kept: list[Found] = []
    for item in ordered:
        if kept and item.start < kept[-1].end:
            continue
        kept.append(item)
    return kept


@dataclass(frozen=True)
class Segment:
    """A piece of redacted text and the original text it stands for."""

    red_start: int
    red_end: int
    orig_start: int
    orig_end: int
    token: bool


@dataclass(frozen=True)
class Redaction:
    text: str
    tokens: dict[str, str] = field(default_factory=dict)  # token -> original value
    segments: tuple[Segment, ...] = ()

    def to_json(self) -> dict[str, object]:
        """For storage, encrypted: it contains the original personal data."""
        return {
            "text": self.text,
            "tokens": self.tokens,
            "segments": [
                [s.red_start, s.red_end, s.orig_start, s.orig_end, s.token] for s in self.segments
            ],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "Redaction":
        segments = tuple(
            Segment(int(a), int(b), int(c), int(d), bool(t)) for a, b, c, d, t in data["segments"]
        )
        return cls(str(data["text"]), {str(k): str(v) for k, v in data["tokens"].items()}, segments)

    def restore(self, value: str) -> str:
        """Put the original values back into a model output (for case fields)."""
        return TOKEN.sub(lambda m: self.tokens.get(m.group(), m.group()), value)

    def to_original(self, start: int, end: int) -> tuple[int, int]:
        """Map ``[start, end)`` in the redacted text to the original text."""
        if not 0 <= start < end <= len(self.text):
            raise ValueError("span outside the redacted text")
        return self._map(start, is_end=False), self._map(end, is_end=True)

    def _map(self, position: int, *, is_end: bool) -> int:
        for seg in self.segments:
            inside = seg.red_start < position < seg.red_end
            at_edge = position == (seg.red_end if is_end else seg.red_start)
            if inside or at_edge:
                if seg.token:
                    return seg.orig_end if is_end else seg.orig_start
                return seg.orig_start + (position - seg.red_start)
        return self.segments[-1].orig_end if self.segments else position


def redact(text: str, known: Sequence[str] = ()) -> Redaction:
    found = find_identifiers(text, known)
    tokens: dict[str, str] = {}
    by_value: dict[tuple[Kind, str], str] = {}
    counters: dict[Kind, int] = {}
    parts: list[str] = []
    segments: list[Segment] = []
    red_pos = 0
    orig_pos = 0

    def plain(until: int) -> None:
        nonlocal red_pos, orig_pos
        if until > orig_pos:
            chunk = text[orig_pos:until]
            parts.append(chunk)
            segments.append(Segment(red_pos, red_pos + len(chunk), orig_pos, until, False))
            red_pos += len(chunk)
            orig_pos = until

    for item in found:
        plain(item.start)
        original = text[item.start : item.end]
        key = (item.kind, original.casefold())
        token = by_value.get(key)
        if token is None:
            counters[item.kind] = counters.get(item.kind, 0) + 1
            token = f"[{item.kind.value}_{counters[item.kind]}]"
            by_value[key] = token
            tokens[token] = original
        parts.append(token)
        segments.append(Segment(red_pos, red_pos + len(token), item.start, item.end, True))
        red_pos += len(token)
        orig_pos = item.end
    plain(len(text))
    return Redaction("".join(parts), tokens, tuple(segments))
