from fastapi.testclient import TestClient

from pharmasafe_api.main import create_app


def test_healthz() -> None:
    response = TestClient(create_app()).get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "service": "api"}
