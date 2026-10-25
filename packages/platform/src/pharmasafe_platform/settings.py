"""Typed runtime settings, read from environment variables prefixed ``PHARMASAFE_``.

A ``.env`` file in the working directory is read too, for local development only.
Real environment variables always win over the file.
"""

from enum import StrEnum
from functools import lru_cache

from pydantic import SecretStr
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

    # Immutable source storage (Azure Blob; Azurite locally). In Azure, Phase 10 replaces
    # the shared key with a managed identity.
    blob_endpoint: str = "http://127.0.0.1:10000/devstoreaccount1"
    blob_account: str = "devstoreaccount1"
    blob_key: SecretStr = SecretStr("")
    blob_container: str = "sources"

    # Email intake by IMAP polling (GreenMail locally). Each tenant's mailbox is in
    # tenants.intake_mailbox; production credentials come from Key Vault.
    imap_host: str = "localhost"
    imap_port: int = 3143
    imap_ssl: bool = False
    imap_password: SecretStr = SecretStr("")

    # Uploads larger than this are refused (bytes, per file).
    intake_max_file_bytes: int = 25 * 1024 * 1024
    # Tesseract executable for OCR of images; OCR is unavailable when it cannot be found.
    tesseract_cmd: str = "tesseract"

    # AI configuration bundles (signed; see pharmasafe_platform.aiconfig). The active
    # version applies to new pipeline runs; every case records the version it used.
    ai_bundle_dir: str = "config/ai_bundles"
    ai_bundle_version: str = "mock-2026.10.1"
    # The AI gateway, the only path to LLM providers. Internal callers present this
    # token; in Azure, mutual TLS between services replaces it (Phase 10).
    ai_gateway_url: str = "http://localhost:8001"
    ai_gateway_token: SecretStr = SecretStr("")
    azure_openai_endpoint: str = ""
    azure_openai_key: SecretStr = SecretStr("")
    openai_base_url: str = "https://api.openai.com/v1"
    openai_api_key: SecretStr = SecretStr("")
    # Self-hosted Langfuse for redacted LLM call logging (disabled when keys are empty).
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: str = ""
    langfuse_secret_key: SecretStr = SecretStr("")
    # Master key (base64, 32 bytes) for per-tenant encryption of redaction mappings.
    # In Azure it comes from Key Vault.
    redaction_key_id: str = "local-1"
    redaction_master_key: SecretStr = SecretStr("")

    @property
    def jwks_url(self) -> str:
        return self.oidc_jwks_url or f"{self.oidc_issuer}/protocol/openid-connect/certs"


@lru_cache
def get_settings() -> Settings:
    return Settings()
