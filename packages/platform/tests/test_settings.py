import pytest

from pharmasafe_platform.settings import Environment, Settings


def test_reads_prefixed_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PHARMASAFE_ENVIRONMENT", "validation")
    monkeypatch.setenv("PHARMASAFE_TEMPORAL_ADDRESS", "temporal:7233")
    settings = Settings()
    assert settings.environment is Environment.VALIDATION
    assert settings.temporal_address == "temporal:7233"
