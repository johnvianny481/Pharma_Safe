"""Shared pytest fixtures.

Database tests run against a real PostgreSQL. Row-level security, grants and triggers
are the point of the tests, and none of them can be faked. Each test session creates a
throwaway database, migrates it as the owner, and connects the code under test as a
non-owner login role in ``pharmasafe_app``, exactly as in production.

Set ``PHARMASAFE_TEST_ADMIN_DATABASE_URL`` (``.env`` locally, a service container in CI).
Without it, database tests are skipped locally and fail in CI.
"""

import os
import secrets
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import jwt
import pytest
from alembic import command
from alembic.config import Config
from cryptography.hazmat.primitives.asymmetric import rsa
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from pharmasafe_platform.auth import Principal, TokenVerifier, principal_from_claims
from pharmasafe_platform.db import Database

ROOT = Path(__file__).parent
TEST_ISSUER = "https://idp.test/realms/pharmasafe"
TEST_AUDIENCE = "pharmasafe-api"
APP_TEST_ROLE = "pharmasafe_app_test"


class _TestEnv(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="PHARMASAFE_", env_file=ROOT / ".env", extra="ignore"
    )
    test_admin_database_url: str | None = None


def _unavailable(reason: str) -> None:
    if os.environ.get("CI"):
        pytest.fail(f"database tests must run in CI: {reason}")
    pytest.skip(reason)


# ---------------------------------------------------------------- database


@dataclass(frozen=True)
class TestDatabase:
    owner_url: str
    app_url: str


@pytest.fixture(scope="session")
def test_database() -> Iterator[TestDatabase]:
    admin_url = _TestEnv().test_admin_database_url
    if not admin_url:
        _unavailable("PHARMASAFE_TEST_ADMIN_DATABASE_URL is not set")
    admin = make_url(admin_url)  # type: ignore[arg-type]
    admin_engine = create_engine(admin, isolation_level="AUTOCOMMIT")
    name = f"pharmasafe_test_{secrets.token_hex(4)}"
    try:
        with admin_engine.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
    except OperationalError as exc:
        admin_engine.dispose()
        _unavailable(f"cannot reach the test PostgreSQL server: {exc.orig}")

    owner = admin.set(database=name)
    try:
        config = Config(str(ROOT / "apps" / "api" / "alembic.ini"))
        config.attributes["database_url"] = owner.render_as_string(hide_password=False)
        config.attributes["configure_logger"] = False
        command.upgrade(config, "head")

        # Random hex only, so it is safe to inline into DDL (which cannot take parameters).
        password = secrets.token_hex(16)
        owner_engine = create_engine(owner, isolation_level="AUTOCOMMIT")
        with owner_engine.connect() as conn:
            exists = conn.execute(
                text("SELECT 1 FROM pg_roles WHERE rolname = :r"), {"r": APP_TEST_ROLE}
            ).first()
            verb = "ALTER" if exists else "CREATE"
            conn.execute(
                text(
                    f"{verb} ROLE {APP_TEST_ROLE} LOGIN NOSUPERUSER NOBYPASSRLS PASSWORD '{password}'"
                )
            )
            conn.execute(text(f"GRANT pharmasafe_app TO {APP_TEST_ROLE}"))
        owner_engine.dispose()

        app = admin.set(database=name, username=APP_TEST_ROLE, password=password)
        yield TestDatabase(
            owner_url=owner.render_as_string(hide_password=False),
            app_url=app.render_as_string(hide_password=False),
        )
    finally:
        with admin_engine.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin_engine.dispose()


@pytest.fixture(scope="session")
def db(test_database: TestDatabase) -> Iterator[Database]:
    """The database as the application sees it: non-owner role, RLS enforced."""
    database = Database(test_database.app_url)
    yield database
    database.dispose()


@pytest.fixture(scope="session")
def owner_engine(test_database: TestDatabase) -> Iterator[Engine]:
    """Schema owner (a superuser locally and in CI). Used to set up and to simulate tampering."""
    engine = create_engine(test_database.owner_url)
    yield engine
    engine.dispose()


@pytest.fixture
def make_tenant(owner_engine: Engine) -> Callable[[], UUID]:
    """Each test gets fresh tenants, because append-only tables can never be cleaned up."""

    def create() -> UUID:
        tenant_id = uuid4()
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO tenants (id, slug, name, region) VALUES (:id, :slug, :name, 'eu')"
                ),
                {"id": tenant_id, "slug": f"t-{tenant_id.hex[:12]}", "name": f"Tenant {tenant_id}"},
            )
        return tenant_id

    return create


@pytest.fixture
def tenant_a(make_tenant: Callable[[], UUID]) -> UUID:
    return make_tenant()


@pytest.fixture
def tenant_b(make_tenant: Callable[[], UUID]) -> UUID:
    return make_tenant()


# ---------------------------------------------------------------- tokens


@pytest.fixture(scope="session")
def signing_key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture(scope="session")
def verifier(signing_key: rsa.RSAPrivateKey) -> TokenVerifier:
    public_key = signing_key.public_key()
    return TokenVerifier(TEST_ISSUER, TEST_AUDIENCE, key_resolver=lambda _token: public_key)


TokenFactory = Callable[..., str]


def default_claims(**overrides: Any) -> dict[str, Any]:
    """Claims shaped like the Keycloak realm's access tokens. ``None`` removes a claim."""
    now = datetime.now(UTC)
    claims: dict[str, Any] = {
        "iss": TEST_ISSUER,
        "aud": TEST_AUDIENCE,
        "sub": "user-1",
        "preferred_username": "user.one",
        "name": "User One",
        "tenant_id": str(UUID(int=1)),
        "roles": ["case_processor"],
        "ps_products": ["*"],
        "ps_regions": ["*"],
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=5)).timestamp()),
        "auth_time": int(now.timestamp()),
        "jti": str(uuid4()),
    }
    claims.update(overrides)
    return {k: v for k, v in claims.items() if v is not None}


@pytest.fixture(scope="session")
def issue_token(signing_key: rsa.RSAPrivateKey) -> TokenFactory:
    def issue(**overrides: Any) -> str:
        return jwt.encode(default_claims(**overrides), signing_key, algorithm="RS256")

    return issue


PrincipalFactory = Callable[..., Principal]


@pytest.fixture(scope="session")
def make_principal() -> PrincipalFactory:
    """A verified-looking principal, built from the same claims a token would carry."""

    def build(**overrides: Any) -> Principal:
        return principal_from_claims(default_claims(**overrides))

    return build
