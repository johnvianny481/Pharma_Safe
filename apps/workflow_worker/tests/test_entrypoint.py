import pytest

from pharmasafe_workflow_worker.__main__ import SERVICE_NAME, main


def test_main_starts_and_logs(capsys: pytest.CaptureFixture[str]) -> None:
    main()
    assert SERVICE_NAME in capsys.readouterr().out
