"""Typed runtime settings, read from environment variables prefixed ``PHARMASAFE_``."""

from enum import StrEnum
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Environment(StrEnum):
    DEV = "dev"
    TEST = "test"
    VALIDATION = "validation"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PHARMASAFE_", extra="ignore")

    environment: Environment = Environment.DEV
    service_name: str = "pharmasafe"
    log_level: str = "INFO"
    database_url: str = "postgresql+psycopg://pharmasafe@localhost:5432/pharmasafe"
    temporal_address: str = "localhost:7233"


@lru_cache
def get_settings() -> Settings:
    return Settings()
