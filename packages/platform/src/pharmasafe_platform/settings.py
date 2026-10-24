"""Typed runtime settings, read from environment variables prefixed ``PHARMASAFE_``.

A ``.env`` file in the working directory is read too, for local development only.
Real environment variables always win over the file.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    VALIDATION = "validation"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PHARMASAFE_", env_file=".env", extra="ignore")

    environment: Environment = Environment.DEV
    service_name: str = "pharmasafe"
    log_level: str = "INFO"

    # Runtime connection: the non-owner application role, always subject to row-level security.
    database_url: str = "postgresql+psycopg://pharmasafe_app_user@localhost:5432/pharmasafe"
    # Schema owner, used only by migrations and operator tooling (seeding), never by requests.
    migration_database_url: str = "postgresql+psycopg://pharmasafe@localhost:5432/pharmasafe"

    temporal_address: str = "localhost:7233"

    oidc_issuer: str = "http://localhost:8180/realms/pharmasafe"
    oidc_audience: str = "pharmasafe-api"
    # Defaults to Keycloak's JWKS path under the issuer when unset.
    oidc_jwks_url: str | None = None

    # Electronic signatures need a re-authentication no older than this (seconds).
    signature_max_auth_age_seconds: int = 300

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
