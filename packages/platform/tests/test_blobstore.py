"""Immutable source storage against Azurite: exact bytes, write-once, integrity checks."""

import secrets

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from pharmasafe_platform.blobstore import (
    AzureBlobStore,
    BlobError,
    BlobIntegrityError,
    BlobNotFound,
    InMemoryBlobStore,
    sha256_hex,
)


@settings(
    max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(data=st.binary(max_size=200_000))
def test_any_bytes_come_back_exactly(blob_store: AzureBlobStore, data: bytes) -> None:
    name = f"t/{sha256_hex(data)}-{secrets.token_hex(4)}"
    stored = blob_store.put_immutable(name, data, "application/octet-stream")
    assert (stored.sha256, stored.size) == (sha256_hex(data), len(data))
    assert blob_store.get_verified(name, stored.sha256) == data


def test_writing_the_same_bytes_again_is_harmless(blob_store: AzureBlobStore) -> None:
    data = b"report \x00\xff"
    name = f"t/{sha256_hex(data)}"
    blob_store.put_immutable(name, data, "text/plain")
    blob_store.put_immutable(name, data, "text/plain")
    assert blob_store.get_verified(name, sha256_hex(data)) == data


def test_a_stored_blob_is_never_overwritten(blob_store: AzureBlobStore) -> None:
    name = f"t/fixed-{secrets.token_hex(4)}"
    blob_store.put_immutable(name, b"original", "text/plain")
    with pytest.raises(BlobIntegrityError):
        blob_store.put_immutable(name, b"replacement", "text/plain")
    assert blob_store.get_verified(name, sha256_hex(b"original")) == b"original"


def test_reading_checks_the_recorded_hash(blob_store: AzureBlobStore) -> None:
    name = f"t/{secrets.token_hex(8)}"
    blob_store.put_immutable(name, b"abc", "text/plain")
    with pytest.raises(BlobIntegrityError):
        blob_store.get_verified(name, sha256_hex(b"abd"))
    with pytest.raises(BlobNotFound):
        blob_store.get_verified("t/missing", sha256_hex(b""))


def test_wrong_key_is_refused(blob_store: AzureBlobStore) -> None:
    impostor = AzureBlobStore(
        "http://127.0.0.1:10000/devstoreaccount1",
        "devstoreaccount1",
        "d3Jvbmcta2V5",
        "sources",
        timeout=5,
    )
    with pytest.raises(BlobError):
        impostor.put_immutable("t/x", b"x", "text/plain")
    impostor.close()


def test_in_memory_store_has_the_same_contract() -> None:
    store = InMemoryBlobStore()
    store.put_immutable("a", b"1", "text/plain")
    store.put_immutable("a", b"1", "text/plain")
    with pytest.raises(BlobIntegrityError):
        store.put_immutable("a", b"2", "text/plain")
    with pytest.raises(BlobNotFound):
        store.get_verified("b", "")
    store.blobs["a"] = b"tampered"
    with pytest.raises(BlobIntegrityError):
        store.get_verified("a", sha256_hex(b"1"))
