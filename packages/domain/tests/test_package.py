import pharmasafe_domain


def test_version_is_exposed() -> None:
    assert pharmasafe_domain.__version__ == "0.1.0"
