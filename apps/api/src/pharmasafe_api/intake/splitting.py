"""Splitting a document that holds several adverse event reports.

Rule-based and conservative: a document is split only when it clearly says it holds
several reports. Signals, in order of precedence:

1. Two or more numbered report headings on their own line, such as ``Report 1``,
   ``Case #2:``, ``Patient 3``, ``Fall 2``, ``Cas n° 4``, ``Informe 5``.
2. Two or more substantial blocks separated by divider lines (``-----``, ``=====``).

Anything else is one report. Wrongly merging two reports is caught by review; wrongly
splitting one report creates duplicates, which is worse. The Phase 5 pipeline can
refine this, but this rule stays as the deterministic baseline.

Spans are character offsets into the extracted text, so every report (and later every
evidence span) points back into its source document.
"""

import itertools
import re
from dataclasses import dataclass

_HEADING_WORDS = (
    "report|case|patient|"  # en
    "bericht|fall|patientin|"  # de
    "rapport|cas|patiente|"  # fr
    "informe|caso|paciente|"  # es, pt
    "segnalazione|paziente|relato"  # it, pt
)
_HEADING = re.compile(
    rf"^[ \t]*(?:#+[ \t]*)?(?:{_HEADING_WORDS})[ \t]*"
    r"(?:#|no\.?|nr\.?|n[o°º]\.?|number)?[ \t]*\d{1,3}[ \t]*[:.)\-\u2013]?[ \t\r]*$",
    re.IGNORECASE | re.MULTILINE,
)
# [ \t\r]*$ in both patterns: email bodies use CRLF line endings, and $ matches only before \n.
_DIVIDER = re.compile(r"^[ \t]*(?:-{5,}|={5,}|\*{5,}|_{5,})[ \t\r]*$", re.MULTILINE)
MIN_BLOCK_CHARS = 40


@dataclass(frozen=True)
class Span:
    start: int
    end: int


def split_reports(text: str) -> list[Span]:
    """Report spans in order. One span for a single report; none for blank text."""
    if not text.strip():
        return []
    headings = [m.start() for m in _HEADING.finditer(text)]
    if len(headings) >= 2:
        bounds = [*headings, len(text)]
        return [_trim(text, a, b) for a, b in itertools.pairwise(bounds)]

    blocks: list[Span] = []
    start = 0
    for divider in _DIVIDER.finditer(text):
        blocks.append(_trim(text, start, divider.start()))
        start = divider.end()
    blocks.append(_trim(text, start, len(text)))
    substantial = [b for b in blocks if _visible_chars(text[b.start : b.end]) >= MIN_BLOCK_CHARS]
    if len(substantial) >= 2:
        return substantial
    return [_trim(text, 0, len(text))]


def _trim(text: str, start: int, end: int) -> Span:
    while start < end and text[start].isspace():
        start += 1
    while end > start and text[end - 1].isspace():
        end -= 1
    return Span(start, end)


def _visible_chars(text: str) -> int:
    return sum(1 for c in text if not c.isspace())
