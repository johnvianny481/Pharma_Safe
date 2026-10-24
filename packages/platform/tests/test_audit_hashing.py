from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from pharmasafe_platform.audit import GENESIS_HASH, entry_hash
from pharmasafe_platform.hashing import canonical_json, sha256_hex

BASE: dict[str, Any] = {
    "tenant_id": UUID(int=1),
    "seq": 1,
    "occurred_at": datetime(2026, 10, 25, 12, 0, tzinfo=UTC),
    "actor_id": "u1",
    "actor_name": "User One",
    "action": "case.field_changed",
    "entity_type": "case",
    "entity_id": "c1",
    "case_version": 1,
    "field": "patient.age",
    "old_value": None,
    "new_value": 42,
    "reason": "corrected from source",
    "prev_hash": GENESIS_HASH,
}

CHANGES: dict[str, Any] = {
    "tenant_id": UUID(int=2),
    "seq": 2,
    "occurred_at": datetime(2026, 10, 25, 12, 0, 0, 1, tzinfo=UTC),
    "actor_id": "u2",
    "actor_name": "User Two",
    "action": "case.created",
    "entity_type": "signature",
    "entity_id": "c2",
    "case_version": 2,
    "field": "patient.sex",
    "old_value": 41,
    "new_value": "42",
    "reason": None,
    "prev_hash": "f" * 64,
}


def test_hash_is_deterministic() -> None:
    assert entry_hash(**BASE) == entry_hash(**BASE)
    assert len(entry_hash(**BASE)) == 64


@pytest.mark.parametrize("field", sorted(CHANGES))
def test_any_field_change_changes_the_hash(field: str) -> None:
    assert entry_hash(**{**BASE, field: CHANGES[field]}) != entry_hash(**BASE)


def test_same_instant_in_another_timezone_hashes_the_same() -> None:
    shifted = BASE["occurred_at"].astimezone(timezone(timedelta(hours=5, minutes=30)))
    assert entry_hash(**{**BASE, "occurred_at": shifted}) == entry_hash(**BASE)


@given(st.dictionaries(st.text(), st.integers() | st.text() | st.booleans() | st.none()))
def test_canonical_json_ignores_key_order(value: dict[str, Any]) -> None:
    reordered = dict(reversed(list(value.items())))
    assert sha256_hex(value) == sha256_hex(reordered)


def test_canonical_json_rejects_nan() -> None:
    with pytest.raises(ValueError, match="JSON compliant"):
        canonical_json({"x": float("nan")})
