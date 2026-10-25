"""Envelope-free field encryption with per-tenant keys (AES-256-GCM).

Used for the redaction mapping (token -> original personal data), which only the case
service may reverse. Each tenant's key is derived from a master key with HKDF, using the
tenant id as context, so one tenant's key never opens another tenant's data. The
associated data binds a ciphertext to its record (for example ``case_id:version``): a
ciphertext copied onto another record does not decrypt.

The master key comes from settings now; in Azure it lives in Key Vault, with an option
for customer-managed keys (Phase 10). ``key_id`` is stored with every ciphertext so the
master key can be rotated without losing old data.
"""

import base64
import os
from dataclasses import dataclass
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

NONCE_BYTES = 12


class DecryptionError(Exception):
    """Wrong key, wrong record, or tampered ciphertext."""


@dataclass(frozen=True)
class Sealed:
    key_id: str
    ciphertext: bytes  # nonce || AES-GCM ciphertext and tag


class TenantCipher:
    def __init__(self, master_keys: dict[str, bytes], current_key_id: str) -> None:
        if current_key_id not in master_keys:
            raise ValueError(f"no master key {current_key_id!r}")
        for key_id, key in master_keys.items():
            if len(key) != 32:
                raise ValueError(f"master key {key_id!r} must be 32 bytes")
        self._keys = master_keys
        self._current = current_key_id

    @classmethod
    def from_base64(cls, key_id: str, key_b64: str) -> "TenantCipher":
        return cls({key_id: base64.b64decode(key_b64)}, key_id)

    def seal(self, tenant_id: UUID, plaintext: bytes, associated: str) -> Sealed:
        nonce = os.urandom(NONCE_BYTES)
        aead = AESGCM(self._tenant_key(self._current, tenant_id))
        body = aead.encrypt(nonce, plaintext, self._aad(tenant_id, associated))
        return Sealed(self._current, nonce + body)

    def open(self, tenant_id: UUID, sealed: Sealed, associated: str) -> bytes:
        if sealed.key_id not in self._keys:
            raise DecryptionError(f"unknown key {sealed.key_id!r}")
        nonce, body = sealed.ciphertext[:NONCE_BYTES], sealed.ciphertext[NONCE_BYTES:]
        aead = AESGCM(self._tenant_key(sealed.key_id, tenant_id))
        try:
            return aead.decrypt(nonce, body, self._aad(tenant_id, associated))
        except InvalidTag as exc:
            raise DecryptionError("ciphertext does not open for this tenant and record") from exc

    def _tenant_key(self, key_id: str, tenant_id: UUID) -> bytes:
        hkdf = HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=b"pharmasafe-tenant-key",
            info=f"tenant:{tenant_id}".encode(),
        )
        return hkdf.derive(self._keys[key_id])

    @staticmethod
    def _aad(tenant_id: UUID, associated: str) -> bytes:
        return f"{tenant_id}|{associated}".encode()
