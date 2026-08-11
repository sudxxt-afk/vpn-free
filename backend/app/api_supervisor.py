"""Run Uvicorn and force a container restart if the API stops responding."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from urllib.request import urlopen


HEALTH_URL = "http://127.0.0.1:8000/health"
STARTUP_SECONDS = int(os.getenv("API_SUPERVISOR_STARTUP_SECONDS", "20"))
CHECK_INTERVAL_SECONDS = int(os.getenv("API_SUPERVISOR_CHECK_INTERVAL_SECONDS", "15"))
MAX_FAILURES = int(os.getenv("API_SUPERVISOR_MAX_FAILURES", "3"))


def healthcheck(timeout: float = 3) -> bool:
    try:
        with urlopen(HEALTH_URL, timeout=timeout) as response:
            return response.status == 200
    except OSError:
        return False


def stop_process(process: subprocess.Popen, timeout: float = 10) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=timeout)


def main() -> int:
    process = subprocess.Popen([
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "0.0.0.0",
        "--port",
        "8000",
        "--workers",
        "2",
        "--timeout-keep-alive",
        "5",
    ])
    stopping = False

    def shutdown(_signum, _frame) -> None:
        nonlocal stopping
        stopping = True
        stop_process(process)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    time.sleep(STARTUP_SECONDS)
    failures = 0
    while not stopping and process.poll() is None:
        if healthcheck():
            failures = 0
        else:
            failures += 1
            print(f"API supervisor health failure {failures}/{MAX_FAILURES}", flush=True)
            if failures >= MAX_FAILURES:
                print("API supervisor is restarting the unresponsive container", flush=True)
                stop_process(process)
                return 1
        time.sleep(CHECK_INTERVAL_SECONDS)

    return process.returncode or 0


if __name__ == "__main__":
    raise SystemExit(main())
