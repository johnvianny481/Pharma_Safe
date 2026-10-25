from datetime import date

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_domain.dates import date_bounds, definitely_before, earliest, latest


@pytest.mark.parametrize(
    ("value", "bounds"),
    [
        ("2024", (date(2024, 1, 1), date(2024, 12, 31))),
        ("2024-02", (date(2024, 2, 1), date(2024, 2, 29))),
        ("2023-02", (date(2023, 2, 1), date(2023, 2, 28))),
        ("2024-07-15", (date(2024, 7, 15), date(2024, 7, 15))),
    ],
)
def test_bounds(value: str, bounds: tuple[date, date]) -> None:
    assert date_bounds(value) == bounds


@pytest.mark.parametrize("value", ["", "24", "2024-1", "2024-13", "2023-02-29", "0000", "x2024"])
def test_rejects_malformed_dates(value: str) -> None:
    with pytest.raises(ValueError):  # noqa: PT011 - the message varies by cause
        date_bounds(value)


def test_definitely_before_only_for_certain_order() -> None:
    assert definitely_before("2024-02", "2024-03-01")
    assert not definitely_before("2024-03", "2024-03-15")  # could be either way
    assert not definitely_before("2024-03-15", "2024-03")
    assert definitely_before(date(2023, 12, 31), "2024")
    assert not definitely_before("2024", date(2024, 6, 1))


@given(st.dates(min_value=date(1, 1, 1)), st.sampled_from([4, 7, 10]))
def test_partial_date_contains_the_day_it_was_cut_from(day: date, length: int) -> None:
    value = day.isoformat()[:length]
    assert earliest(value) <= day <= latest(value)


@given(st.dates(), st.dates())
def test_full_dates_compare_like_dates(a: date, b: date) -> None:
    assert definitely_before(a.isoformat(), b.isoformat()) == (a < b)
