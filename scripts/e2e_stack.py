"""Start the application processes for the Playwright end-to-end tests.

Assumes the infrastructure is up (``make up``: Postgres, Temporal, Keycloak, Azurite).
Then, in order:

1. applies migrations and seeds the demo tenants (both idempotent);
2. starts the API (:8000), the AI gateway (:8001, MockLLM bundle), the case workflow
   worker and the AI pipeline worker;
3. waits until the API and the gateway answer ``/healthz``, then prints ``E2E stack ready``;
4. runs until it is stopped, then stops every process it started.

Playwright starts this as a ``webServer`` and stops it after the tests.
"""

import os
import signal
import subprocess  # nosec B404
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PYTHON = [sys.executable]
SERVICES = {
    "api": [*PYTHON, "-m", "uvicorn", "pharmasafe_api.main:app", "--port", "8000"],
    "ai-gateway": [*PYTHON, "-m", "uvicorn", "pharmasafe_ai_gateway.main:app", "--port", "8001"],
    "workflow-worker": [*PYTHON, "-m", "pharmasafe_workflow_worker", "--relay-interval", "0.5"],
    "ai-worker": [*PYTHON, "-m", "pharmasafe_ai_worker"],
}
HEALTH = ["http://127.0.0.1:8000/healthz", "http://127.0.0.1:8001/healthz"]


def _run(*args: str) -> None:
    # Fixed argv from this file, no shell.
    subprocess.run([*PYTHON, *args], cwd=ROOT, check=True)  # noqa: S603  # nosec B603


def _healthy(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310  # nosec B310
            return bool(response.status == 200)
    except OSError:
        return False


def main() -> None:
    _run("-m", "alembic", "-c", "apps/api/alembic.ini", "upgrade", "head")
    _run("-m", "pharmasafe_api.tenancy.seed")
    env = os.environ | {"PYTHONUNBUFFERED": "1"}
    processes = [
        subprocess.Popen(command, cwd=ROOT, env=env)  # noqa: S603  # nosec B603
        for command in SERVICES.values()
    ]

    def stop(*_: object) -> None:
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    deadline = time.monotonic() + 120
    while not all(_healthy(url) for url in HEALTH):
        if time.monotonic() > deadline or any(p.poll() is not None for p in processes):
            stop()
        time.sleep(1)
    print("E2E stack ready", flush=True)
    while all(p.poll() is None for p in processes):
        time.sleep(1)
    stop()  # one process died: stop the rest so the tests fail clearly


if __name__ == "__main__":
    main()
