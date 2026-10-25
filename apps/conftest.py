"""Fixtures for the AI gateway and pipeline, shared by the app test suites.

- ``bundle_store``: signed bundles. ``committed`` uses the bundles in ``config/ai_bundles``
  (signed with the committed public key); ``make_bundles`` signs test variants with a key
  made for the test session, for outage and compromised-model scenarios.
- ``gateway``: the gateway service in process, with MockLLM and the real call ledger.
- ``pipeline_deps``: everything the AI worker's activities need.
"""

import base64
import itertools
import json
import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from pharmasafe_ai_gateway.langfuse import Generation
from pharmasafe_ai_gateway.ledger import Ledger
from pharmasafe_ai_gateway.mock import MockLLM
from pharmasafe_ai_gateway.service import GatewayService
from pharmasafe_ai_worker.activities import Deps
from pharmasafe_ai_worker.dictionary import SyntheticDictionary
from pharmasafe_domain.rules.clocks import seed_registry
from pharmasafe_platform.aiconfig import BUNDLE_FILE, SIGNATURE_FILE, BundleStore, sign
from pharmasafe_platform.db import Database
from pharmasafe_platform.encryption import TenantCipher

BUNDLE_ROOT = Path(__file__).parent.parent / "config" / "ai_bundles"
MOCK_BUNDLE = "mock-2026.10.1"
TEST_KEY_ID = "test-session"


class RecordingLog:
    def __init__(self) -> None:
        self.generations: list[Generation] = []

    def record(self, generation: Generation) -> None:
        self.generations.append(generation)


def mock_target(model: str = "mock-pv-v1") -> dict[str, Any]:
    return {"provider": "mock", "model": model, "usd_per_1k_input": 0.0001,
            "usd_per_1k_output": 0.0002}  # fmt: skip


@pytest.fixture(scope="session")
def committed_bundles() -> BundleStore:
    return BundleStore(BUNDLE_ROOT)


@pytest.fixture(scope="session")
def bundle_template() -> dict[str, Any]:
    data: dict[str, Any] = json.loads((BUNDLE_ROOT / MOCK_BUNDLE / BUNDLE_FILE).read_text("utf-8"))
    return data


@pytest.fixture(scope="session")
def make_bundles(
    tmp_path_factory: pytest.TempPathFactory, bundle_template: dict[str, Any]
) -> Callable[..., tuple[BundleStore, str]]:
    """``make_bundles(name, primary=..., fallback=..., **route)`` -> (store, unique version)."""
    root = tmp_path_factory.mktemp("bundles")
    key = Ed25519PrivateKey.generate()
    store = BundleStore(root, trusted_keys={TEST_KEY_ID: key.public_key()})
    made = itertools.count(1)

    def make(
        version: str,
        *,
        primary: str = "mock-pv-v1",
        fallback: str | None = "mock-pv-v1",
        thresholds: dict[str, float] | None = None,
        **route: Any,
    ) -> tuple[BundleStore, str]:
        data = json.loads(json.dumps(bundle_template))
        # A fresh version every time: the store caches bundles by version, so reusing a
        # name with other settings would silently return the first bundle.
        version = f"{version}.{next(made)}"
        data["version"] = version
        data["thresholds"].update(thresholds or {})
        for step in data["routes"]:
            data["routes"][step].update(
                primary=mock_target(primary),
                fallback=mock_target(fallback) if fallback else None,
                **route,
            )
        directory = root / version
        directory.mkdir()
        (directory / BUNDLE_FILE).write_text(json.dumps(data), encoding="utf-8")
        signature = sign(data, key, TEST_KEY_ID)
        (directory / SIGNATURE_FILE).write_text(signature.model_dump_json(), encoding="utf-8")
        return store, version

    return make


@pytest.fixture
def call_log() -> RecordingLog:
    return RecordingLog()


@pytest.fixture
def make_gateway(db: Database, call_log: RecordingLog) -> Callable[..., GatewayService]:
    def make(bundles: BundleStore, *, budget: Decimal = Decimal("500")) -> GatewayService:
        return GatewayService(
            bundles, {"mock": MockLLM()}, Ledger(db, budget), call_log, sleep=lambda _s: None
        )

    return make


@pytest.fixture(scope="session")
def cipher() -> TenantCipher:
    return TenantCipher.from_base64("test-1", base64.b64encode(os.urandom(32)).decode())


@pytest.fixture
def make_deps(
    db: Database, cipher: TenantCipher, make_gateway: Callable[..., GatewayService]
) -> Callable[..., Deps]:
    def make(bundles: BundleStore, version: str, **gateway: Any) -> Deps:
        return Deps(
            database=db,
            gateway=make_gateway(bundles, **gateway),
            bundles=bundles,
            bundle_version=version,
            cipher=cipher,
            dictionary=SyntheticDictionary(),
            registry=seed_registry(),
        )

    return make


@pytest.fixture
def pipeline_deps(committed_bundles: BundleStore, make_deps: Callable[..., Deps]) -> Deps:
    return make_deps(committed_bundles, MOCK_BUNDLE)
