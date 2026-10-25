"""Per-tenant encryption of redaction mappings."""

import os
from uuid import uuid4

import pytest

from pharmasafe_platform.encryption import DecryptionError, Sealed, TenantCipher

TENANT = uuid4()


def _cipher() -> TenantCipher:
    return TenantCipher({"k1": os.urandom(32)}, "k1")


def test_round_trip() -> None:
    cipher = _cipher()
    sealed = cipher.seal(TENANT, b"[NAME_1] = Maria Silva", "case:1")
    assert b"Maria" not in sealed.ciphertext
    assert cipher.open(TENANT, sealed, "case:1") == b"[NAME_1] = Maria Silva"


def test_another_tenant_cannot_open_it() -> None:
    cipher = _cipher()
    sealed = cipher.seal(TENANT, b"secret", "case:1")
    with pytest.raises(DecryptionError):
        cipher.open(uuid4(), sealed, "case:1")


def test_a_ciphertext_moved_to_another_record_does_not_open() -> None:
    cipher = _cipher()
    sealed = cipher.seal(TENANT, b"secret", "case:1")
    with pytest.raises(DecryptionError):
        cipher.open(TENANT, sealed, "case:2")


def test_tampering_is_detected() -> None:
    cipher = _cipher()
    sealed = cipher.seal(TENANT, b"secret", "case:1")
    flipped = bytes([sealed.ciphertext[-1] ^ 1])
    with pytest.raises(DecryptionError):
        cipher.open(TENANT, Sealed("k1", sealed.ciphertext[:-1] + flipped), "case:1")


def test_old_keys_still_open_after_rotation() -> None:
    old_key, new_key = os.urandom(32), os.urandom(32)
    sealed = TenantCipher({"k1": old_key}, "k1").seal(TENANT, b"secret", "case:1")
    rotated = TenantCipher({"k1": old_key, "k2": new_key}, "k2")
    assert rotated.open(TENANT, sealed, "case:1") == b"secret"
    assert rotated.seal(TENANT, b"x", "case:1").key_id == "k2"
    with pytest.raises(DecryptionError, match="unknown key"):
        TenantCipher({"k2": new_key}, "k2").open(TENANT, sealed, "case:1")


def test_keys_must_be_256_bit() -> None:
    with pytest.raises(ValueError, match="32 bytes"):
        TenantCipher({"k1": b"short"}, "k1")
    with pytest.raises(ValueError, match="no master key"):
        TenantCipher({"k1": os.urandom(32)}, "k2")
