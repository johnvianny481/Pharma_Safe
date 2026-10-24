"""Canonical JSON hashing used by the audit ledger and e-signatures.

The same logical content must always give the same hash, so keys are sorted, separators
are fixed, and non-finite floats (which JSON cannot represent) are rejected.
"""

import hashlib
import json
from typing import Any

HASH_HEX_LENGTH = 64


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
