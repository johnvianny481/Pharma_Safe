"""Partial dates, as E2B(R3) allows them: ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``.

Reporters often know only "March 2026" or "2025". A partial date stands for a range of
days, so comparisons ask whether one date is *definitely* before another (the whole of
one range ends before the other starts). The rules flag only definite contradictions,
never ones that a more precise date could resolve.
"""

import calendar
import re
from datetime import date
from typing import Annotated

from pydantic import AfterValidator

_PATTERN = re.compile(r"^(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?$")


def date_bounds(value: str) -> tuple[date, date]:
    """The first and last day a partial date could mean."""
    match = _PATTERN.match(value)
    if match is None:
        raise ValueError("expected YYYY, YYYY-MM or YYYY-MM-DD")
    year = int(match[1])
    if year < 1:
        raise ValueError("year must be 0001 or later")
    if match[2] is None:
        return date(year, 1, 1), date(year, 12, 31)
    month = int(match[2])
    if not 1 <= month <= 12:
        raise ValueError("month must be 01-12")
    if match[3] is None:
        return date(year, month, 1), date(year, month, calendar.monthrange(year, month)[1])
    day = date(year, month, int(match[3]))  # raises ValueError for impossible days
    return day, day


def _validate(value: str) -> str:
    date_bounds(value)
    return value


PartialDate = Annotated[str, AfterValidator(_validate)]


def earliest(value: str) -> date:
    return date_bounds(value)[0]


def latest(value: str) -> date:
    return date_bounds(value)[1]


def definitely_before(a: str | date, b: str | date) -> bool:
    """True only if every day ``a`` could mean is before every day ``b`` could mean."""
    return _latest(a) < _earliest(b)


def _earliest(value: str | date) -> date:
    return value if isinstance(value, date) else earliest(value)


def _latest(value: str | date) -> date:
    return value if isinstance(value, date) else latest(value)
