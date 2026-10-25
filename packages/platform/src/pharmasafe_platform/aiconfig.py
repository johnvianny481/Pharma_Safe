"""The AI configuration bundle: prompts, models, thresholds and retrieval settings,
released together as one versioned, signed unit.

A bundle is a directory ``config/ai_bundles/<version>/`` holding ``bundle.json`` and
``bundle.sig``. The signature is Ed25519 over the canonical JSON (sorted keys, no
whitespace), made under change control with a release key. Services trust only the
public keys in ``config/ai_bundles/trusted_keys/`` and refuse a bundle that is unsigned,
signed by an unknown key, altered after signing, or stored under a different version.

**Pinned models.** Every route names an exact model version: a dated snapshot such as
``gpt-4o-2024-08-06``, plus the deployment and API version for Azure. A floating alias
(``gpt-4o``, ``latest``) is refused, so a provider cannot change the model silently.

Every case records the version of the bundle that processed it, and rolling back is
switching the active version back to the previous approved bundle.

CLI (operator tooling)::

    python -m pharmasafe_platform.aiconfig keygen <private-key-path> <public-key-path>
    python -m pharmasafe_platform.aiconfig sign <bundle-dir> <private-key-path> <key-id>
    python -m pharmasafe_platform.aiconfig verify <bundle-dir>
"""

import argparse
import base64
import json
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

BUNDLE_FILE = "bundle.json"
SIGNATURE_FILE = "bundle.sig"
TRUSTED_KEYS_DIR = "trusted_keys"

_DATED_SNAPSHOT = re.compile(r"^[a-z0-9.-]+-\d{4}-\d{2}-\d{2}$")
_MOCK_MODEL = re.compile(r"^mock-[a-z0-9-]+-v\d+$")
_API_VERSION = re.compile(r"^\d{4}-\d{2}-\d{2}(-preview)?$")


class BundleError(Exception):
    """The bundle is missing, malformed, unsigned or not trusted."""


class Step(StrEnum):
    """The AI pipeline steps that call a model."""

    EXTRACTION = "extraction"
    SERIOUSNESS = "seriousness"
    CODING = "coding"
    FOLLOW_UP = "follow_up"
    NARRATIVE = "narrative"


class ProviderName(StrEnum):
    AZURE_OPENAI = "azure_openai"
    OPENAI = "openai"
    MOCK = "mock"


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ModelTarget(_Frozen):
    """One provider and one exact model version."""

    provider: ProviderName
    model: str = Field(min_length=1, max_length=100)
    # Azure OpenAI only: the deployment serving ``model``, and the pinned API version.
    deployment: str | None = None
    api_version: str | None = None
    usd_per_1k_input: float = Field(ge=0)
    usd_per_1k_output: float = Field(ge=0)

    @model_validator(mode="after")
    def _pinned(self) -> Self:
        if self.provider is ProviderName.MOCK:
            if not _MOCK_MODEL.match(self.model):
                raise ValueError(f"mock model {self.model!r} must look like mock-<name>-v<n>")
            return self
        if not _DATED_SNAPSHOT.match(self.model):
            raise ValueError(f"model {self.model!r} is not pinned to a dated snapshot")
        if self.provider is ProviderName.AZURE_OPENAI:
            if not self.deployment:
                raise ValueError("an Azure OpenAI target needs its deployment")
            if not self.api_version or not _API_VERSION.match(self.api_version):
                raise ValueError("an Azure OpenAI target needs a pinned api_version")
        return self


class Route(_Frozen):
    """How one step calls a model: primary, fallback and limits."""

    primary: ModelTarget
    fallback: ModelTarget | None = None
    max_tokens: int = Field(ge=1, le=16000)
    max_input_chars: int = Field(ge=1, le=200_000)
    timeout_seconds: float = Field(gt=0, le=300)
    # Attempts per target, with exponential backoff between them.
    max_attempts: int = Field(ge=1, le=5)
    temperature: float = Field(default=0.0, ge=0, le=1)


class Prompt(_Frozen):
    version: str = Field(min_length=1, max_length=100)
    system: str = Field(min_length=1)
    # Rendered with str.format_map; the report text is always inserted pre-delimited.
    user: str = Field(min_length=1)


class Thresholds(_Frozen):
    # Fields below this confidence are highlighted for review.
    low_confidence: float = Field(ge=0, le=1)
    # L2 straight-through needs every AI field at or above this.
    straight_through_confidence: float = Field(ge=0, le=1)
    # Duplicate candidates scoring below this are not reported.
    duplicate_min_score: float = Field(ge=0, le=1)


class Retrieval(_Frozen):
    coding_candidates: int = Field(ge=1, le=50)
    duplicate_block_limit: int = Field(ge=1, le=1000)
    duplicate_top: int = Field(ge=1, le=10)
    ngram: int = Field(ge=2, le=5)
    dimensions: int = Field(ge=16, le=2000)


class Bundle(_Frozen):
    version: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    description: str = ""
    prompts: dict[Step, Prompt]
    routes: dict[Step, Route]
    thresholds: Thresholds
    retrieval: Retrieval

    @model_validator(mode="after")
    def _every_step_configured(self) -> Self:
        missing = [s.value for s in Step if s not in self.prompts or s not in self.routes]
        if missing:
            raise ValueError(f"steps without a prompt or route: {', '.join(missing)}")
        return self


class Signature(_Frozen):
    key_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    algorithm: Literal["ed25519"] = "ed25519"
    signature: str


def canonical(data: Any) -> bytes:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def sign(bundle_data: dict[str, Any], key: Ed25519PrivateKey, key_id: str) -> Signature:
    raw = key.sign(canonical(bundle_data))
    return Signature(key_id=key_id, signature=base64.b64encode(raw).decode())


class BundleStore:
    """Loads and verifies bundles from a directory of bundle directories."""

    def __init__(self, root: Path, trusted_keys: dict[str, Ed25519PublicKey] | None = None):
        self._root = root
        self._keys = trusted_keys if trusted_keys is not None else load_trusted_keys(root)
        self._cache: dict[str, Bundle] = {}

    def get(self, version: str) -> Bundle:
        if version not in self._cache:
            self._cache[version] = self._load(version)
        return self._cache[version]

    def _load(self, version: str) -> Bundle:
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", version) or version.startswith("."):
            raise BundleError(f"invalid bundle version {version!r}")
        directory = self._root / version
        try:
            data = json.loads((directory / BUNDLE_FILE).read_text(encoding="utf-8"))
            signature = Signature.model_validate_json(
                (directory / SIGNATURE_FILE).read_text(encoding="utf-8")
            )
        except FileNotFoundError as exc:
            raise BundleError(f"bundle {version} is missing {Path(exc.filename).name}") from exc
        except (ValueError, ValidationError) as exc:
            raise BundleError(f"bundle {version} is malformed: {exc}") from exc
        verify(data, signature, self._keys)
        try:
            bundle = Bundle.model_validate(data)
        except ValidationError as exc:
            raise BundleError(f"bundle {version} is invalid: {exc}") from exc
        if bundle.version != version:
            raise BundleError(f"bundle in {version}/ declares version {bundle.version}")
        return bundle


def verify(data: Any, signature: Signature, keys: dict[str, Ed25519PublicKey]) -> None:
    key = keys.get(signature.key_id)
    if key is None:
        raise BundleError(f"bundle is signed by an untrusted key {signature.key_id!r}")
    try:
        key.verify(base64.b64decode(signature.signature), canonical(data))
    except (InvalidSignature, ValueError) as exc:
        raise BundleError("bundle signature does not match its content") from exc


def load_trusted_keys(root: Path) -> dict[str, Ed25519PublicKey]:
    """Public keys ``trusted_keys/<key-id>.pub`` (PEM)."""
    keys: dict[str, Ed25519PublicKey] = {}
    for path in sorted((root / TRUSTED_KEYS_DIR).glob("*.pub")):
        key = serialization.load_pem_public_key(path.read_bytes())
        if not isinstance(key, Ed25519PublicKey):
            raise BundleError(f"{path.name} is not an Ed25519 public key")
        keys[path.stem] = key
    return keys


def _main() -> None:
    parser = argparse.ArgumentParser(description="AI configuration bundle tooling")
    commands = parser.add_subparsers(dest="command", required=True)
    keygen = commands.add_parser("keygen")
    keygen.add_argument("private_key", type=Path)
    keygen.add_argument("public_key", type=Path)
    signer = commands.add_parser("sign")
    signer.add_argument("bundle_dir", type=Path)
    signer.add_argument("private_key", type=Path)
    signer.add_argument("key_id")
    checker = commands.add_parser("verify")
    checker.add_argument("bundle_dir", type=Path)
    args = parser.parse_args()

    if args.command == "keygen":
        key = Ed25519PrivateKey.generate()
        args.private_key.parent.mkdir(parents=True, exist_ok=True)
        args.private_key.write_bytes(
            key.private_bytes(
                serialization.Encoding.PEM,
                serialization.PrivateFormat.PKCS8,
                serialization.NoEncryption(),
            )
        )
        args.public_key.write_bytes(
            key.public_key().public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
        )
    elif args.command == "sign":
        private = serialization.load_pem_private_key(args.private_key.read_bytes(), None)
        if not isinstance(private, Ed25519PrivateKey):
            raise SystemExit("not an Ed25519 private key")
        data = json.loads((args.bundle_dir / BUNDLE_FILE).read_text(encoding="utf-8"))
        Bundle.model_validate(data)  # never sign an invalid bundle
        signature = sign(data, private, args.key_id)
        (args.bundle_dir / SIGNATURE_FILE).write_text(
            signature.model_dump_json(indent=1) + "\n", encoding="utf-8"
        )
    else:
        directory: Path = args.bundle_dir.resolve()
        bundle = BundleStore(directory.parent).get(directory.name)
        print(f"bundle {bundle.version}: signature valid, {len(bundle.routes)} routes")


if __name__ == "__main__":
    _main()
