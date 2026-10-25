"""Immutable source storage on Azure Blob Storage (Azurite locally).

This is a small client for the Blob REST API, written by hand with SharedKey
authorisation instead of the Azure SDK (ADR 0012). It does only what intake needs:

- create the container if missing;
- write a blob *once*: ``If-None-Match: *`` makes the service refuse to overwrite;
- read a blob and prove it is unchanged against the SHA-256 recorded at intake.

Blobs are content-addressed (``{tenant_id}/{sha256}``), so writing the same bytes twice
is harmless and never replaces anything. The application never deletes blobs. In Azure,
the container also has a version-level WORM immutability policy and the app identity has
no delete permission (Terraform, Phase 10); Azurite does not emulate those policies.
"""

import base64
import hashlib
import hmac
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Protocol
from urllib.parse import parse_qsl, urlsplit

import httpx

API_VERSION = "2024-08-04"


class BlobError(Exception):
    pass


class BlobNotFound(BlobError):
    pass


class BlobIntegrityError(BlobError):
    """Stored bytes do not match the hash recorded when they were received."""


@dataclass(frozen=True)
class StoredBlob:
    name: str
    sha256: str
    size: int


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class BlobStore(Protocol):
    def put_immutable(self, name: str, data: bytes, content_type: str) -> StoredBlob: ...

    def get_verified(self, name: str, sha256: str) -> bytes: ...


class AzureBlobStore:
    """``endpoint`` is the account's blob URL, e.g. ``https://acct.blob.core.windows.net``
    or, for Azurite, ``http://127.0.0.1:10000/devstoreaccount1``."""

    def __init__(
        self,
        endpoint: str,
        account: str,
        key: str,
        container: str,
        *,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._account = account
        self._key = base64.b64decode(key)
        self._container = container
        self._client = httpx.Client(timeout=timeout, transport=transport)
        self._container_ready = False

    def close(self) -> None:
        self._client.close()

    def ensure_container(self) -> None:
        response = self._request("PUT", f"{self._container}?restype=container")
        if response.status_code not in (201, 409):  # 409: it already exists
            raise BlobError(f"cannot create container: HTTP {response.status_code}")
        self._container_ready = True

    def put_immutable(self, name: str, data: bytes, content_type: str) -> StoredBlob:
        if not self._container_ready:
            self.ensure_container()
        digest = sha256_hex(data)
        md5 = base64.b64encode(hashlib.md5(data, usedforsecurity=False).digest()).decode()
        response = self._request(
            "PUT",
            f"{self._container}/{name}",
            content=data,
            headers={
                "Content-Type": content_type,
                "Content-MD5": md5,  # the service rejects the upload if bytes were corrupted
                "If-None-Match": "*",  # never overwrite
                "x-ms-blob-type": "BlockBlob",
                "x-ms-meta-sha256": digest,
            },
        )
        if response.status_code == 409:
            # Already stored. Content-addressed names mean it should be the same bytes;
            # prove it rather than assume it.
            self.get_verified(name, digest)
        elif response.status_code != 201:
            raise BlobError(f"cannot store blob: HTTP {response.status_code}")
        return StoredBlob(name=name, sha256=digest, size=len(data))

    def get_verified(self, name: str, sha256: str) -> bytes:
        response = self._request("GET", f"{self._container}/{name}")
        if response.status_code == 404:
            raise BlobNotFound(name)
        if response.status_code != 200:
            raise BlobError(f"cannot read blob: HTTP {response.status_code}")
        data = response.content
        if sha256_hex(data) != sha256:
            raise BlobIntegrityError(f"{name} does not match its recorded SHA-256")
        return data

    # ------------------------------------------------------------ SharedKey requests

    def _request(
        self,
        method: str,
        path: str,
        *,
        content: bytes = b"",
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        url = f"{self._endpoint}/{path}"
        all_headers = {
            "x-ms-date": format_datetime(datetime.now(UTC), usegmt=True),
            "x-ms-version": API_VERSION,
            **(headers or {}),
        }
        if method == "PUT":
            all_headers["Content-Length"] = str(len(content))
        all_headers["Authorization"] = (
            f"SharedKey {self._account}:{self._signature(method, url, all_headers)}"
        )
        try:
            return self._client.request(method, url, content=content, headers=all_headers)
        except httpx.HTTPError as exc:
            raise BlobError(f"blob service unreachable: {type(exc).__name__}") from exc

    def _signature(self, method: str, url: str, headers: dict[str, str]) -> str:
        """SharedKey signature (Blob service, version 2015-02-21 and later)."""
        h = {k.lower(): v for k, v in headers.items()}
        length = h.get("content-length", "")
        string_to_sign = "\n".join(
            [
                method,
                h.get("content-encoding", ""),
                h.get("content-language", ""),
                "" if length == "0" else length,
                h.get("content-md5", ""),
                h.get("content-type", ""),
                "",  # Date: x-ms-date is used instead
                h.get("if-modified-since", ""),
                h.get("if-match", ""),
                h.get("if-none-match", ""),
                h.get("if-unmodified-since", ""),
                h.get("range", ""),
            ]
        )
        canonical_headers = "".join(
            f"{k}:{v.strip()}\n" for k, v in sorted(h.items()) if k.startswith("x-ms-")
        )
        parts = urlsplit(url)
        # The path already includes the account name for Azurite's path-style URLs, and the
        # service expects it twice in that case, exactly as written here.
        resource = f"/{self._account}{parts.path}"
        for name, value in sorted(parse_qsl(parts.query)):
            resource += f"\n{name.lower()}:{value}"
        payload = f"{string_to_sign}\n{canonical_headers}{resource}".encode()
        digest = hmac.new(self._key, payload, hashlib.sha256).digest()
        return base64.b64encode(digest).decode()


class InMemoryBlobStore:
    """Same contract as :class:`AzureBlobStore`, for unit tests."""

    def __init__(self) -> None:
        self.blobs: dict[str, bytes] = {}

    def put_immutable(self, name: str, data: bytes, content_type: str) -> StoredBlob:
        digest = sha256_hex(data)
        existing = self.blobs.setdefault(name, data)
        if sha256_hex(existing) != digest:
            raise BlobIntegrityError(f"{name} already holds different bytes")
        return StoredBlob(name=name, sha256=digest, size=len(data))

    def get_verified(self, name: str, sha256: str) -> bytes:
        if name not in self.blobs:
            raise BlobNotFound(name)
        data = self.blobs[name]
        if sha256_hex(data) != sha256:
            raise BlobIntegrityError(f"{name} does not match its recorded SHA-256")
        return data
