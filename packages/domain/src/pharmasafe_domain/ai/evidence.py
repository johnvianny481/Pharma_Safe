"""Evidence checks: an AI value counts only if its quote is really in the source.

The model returns a value and a verbatim quote. :func:`check_evidence` finds the quote in
the (redacted) text the model was given and, for free-text values, checks that the value
is actually in the quote. Anything else is rejected with a reason, never "repaired": a
value without valid evidence never reaches the case.
"""

import re
import unicodedata
from dataclasses import dataclass

MAX_QUOTE_CHARS = 500


@dataclass(frozen=True)
class Located:
    start: int
    end: int


@dataclass(frozen=True)
class Rejection:
    path: str
    reason: str


def locate(quote: str, text: str, *, near: int = 0) -> Located | None:
    """The exact quote's position (the first one at or after ``near``, else the first)."""
    if not quote or not quote.strip() or len(quote) > MAX_QUOTE_CHARS:
        return None
    index = text.find(quote, near)
    if index < 0:
        index = text.find(quote)
    if index < 0:
        return None
    return Located(index, index + len(quote))


def supports(value: str, quote: str) -> bool:
    """Whether every word of a free-text value appears in the quote (case-insensitive)."""
    words = _words(value)
    return bool(words) and set(words) <= set(_words(quote))


def check_evidence(
    path: str, quote: str | None, text: str, *, value: str | None = None
) -> Located | Rejection:
    """Locate the quote, and if ``value`` is given, check that the quote supports it."""
    if not quote:
        return Rejection(path, "no evidence quoted")
    located = locate(quote, text)
    if located is None:
        return Rejection(path, "quoted evidence is not in the source text")
    if value is not None and not supports(value, quote):
        return Rejection(path, "the quoted evidence does not contain the value")
    return located


def _words(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", value).casefold()
    return re.findall(r"\w+", folded)
