"""AI bundles: signed, pinned, and refused when anything is off."""

import json
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pharmasafe_platform.aiconfig import (
    BUNDLE_FILE,
    SIGNATURE_FILE,
    Bundle,
    BundleError,
    BundleStore,
    ModelTarget,
    sign,
)

ROOT = Path(__file__).parents[3] / "config" / "ai_bundles"


def _data(version: str = "1.0") -> dict[str, Any]:
    data: dict[str, Any] = json.loads((ROOT / "2026.10.1" / BUNDLE_FILE).read_text("utf-8"))
    data["version"] = version
    return data


def _write(root: Path, data: dict[str, Any], key: Ed25519PrivateKey, key_id: str = "k1") -> None:
    directory = root / data["version"]
    directory.mkdir(parents=True, exist_ok=True)
    (directory / BUNDLE_FILE).write_text(json.dumps(data), encoding="utf-8")
    (directory / SIGNATURE_FILE).write_text(sign(data, key, key_id).model_dump_json(), "utf-8")


def test_committed_bundles_are_signed_by_the_committed_key() -> None:
    store = BundleStore(ROOT)
    for version in ("2026.10.1", "mock-2026.10.1"):
        bundle = store.get(version)
        assert bundle.version == version
        assert bundle.routes
        assert bundle.prompts


def test_production_bundle_pins_model_versions() -> None:
    bundle = BundleStore(ROOT).get("2026.10.1")
    for route in bundle.routes.values():
        assert route.primary.model == "gpt-4o-2024-08-06"
        assert route.primary.api_version == "2024-10-21"
        assert route.fallback is not None
        assert route.fallback.provider.value == "openai"


def test_a_signed_bundle_loads(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    _write(tmp_path, _data(), key)
    assert BundleStore(tmp_path, {"k1": key.public_key()}).get("1.0").version == "1.0"


def test_tampering_after_signing_is_refused(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    _write(tmp_path, _data(), key)
    path = tmp_path / "1.0" / BUNDLE_FILE
    data = json.loads(path.read_text("utf-8"))
    data["thresholds"]["low_confidence"] = 0.0
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(BundleError, match="does not match"):
        BundleStore(tmp_path, {"k1": key.public_key()}).get("1.0")


def test_unknown_signers_and_missing_signatures_are_refused(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    _write(tmp_path, _data(), key, key_id="stranger")
    with pytest.raises(BundleError, match="untrusted key"):
        BundleStore(tmp_path, {"k1": key.public_key()}).get("1.0")
    (tmp_path / "1.0" / SIGNATURE_FILE).unlink()
    with pytest.raises(BundleError, match=r"missing bundle.sig"):
        BundleStore(tmp_path, {"k1": key.public_key()}).get("1.0")


def test_a_bundle_must_live_under_its_own_version(tmp_path: Path) -> None:
    key = Ed25519PrivateKey.generate()
    data = _data("2.0")
    _write(tmp_path, data, key)
    (tmp_path / "2.0").rename(tmp_path / "3.0")
    with pytest.raises(BundleError, match=r"declares version 2.0"):
        BundleStore(tmp_path, {"k1": key.public_key()}).get("3.0")
    with pytest.raises(BundleError, match="invalid bundle version"):
        BundleStore(tmp_path, {}).get("../etc")


@pytest.mark.parametrize(
    ("target", "message"),
    [
        ({"provider": "openai", "model": "gpt-4o"}, "not pinned"),
        ({"provider": "openai", "model": "latest"}, "not pinned"),
        ({"provider": "azure_openai", "model": "gpt-4o-2024-08-06"}, "deployment"),
        (
            {"provider": "azure_openai", "model": "gpt-4o-2024-08-06", "deployment": "d"},
            "api_version",
        ),
        ({"provider": "mock", "model": "anything"}, "mock-<name>-v<n>"),
    ],
)
def test_floating_or_incomplete_model_targets_are_refused(
    target: dict[str, Any], message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        ModelTarget.model_validate(target | {"usd_per_1k_input": 0, "usd_per_1k_output": 0})


def test_every_step_needs_a_prompt_and_a_route() -> None:
    data = _data()
    del data["routes"]["narrative"]
    with pytest.raises(ValueError, match="narrative"):
        Bundle.model_validate(data)
