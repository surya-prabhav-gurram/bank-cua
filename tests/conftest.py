import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

ROOT = os.path.abspath(os.path.dirname(__file__) + "/..")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _healthz(base: str) -> bool:
    """True only if OUR mock answers — not just any process on the port.

    (macOS AirPlay Receiver squats on :5000 and returns 403, so a plain
    port-open check is not enough; we verify the healthz contract.)
    """
    try:
        with urllib.request.urlopen(f"{base}/healthz", timeout=0.5) as r:
            return json.load(r).get("ok") is True
    except Exception:
        return False


@pytest.fixture(scope="session")
def mock_app():
    """Start the mock bank app on a free port using THIS interpreter, verified
    via /healthz. Skips cleanly if it can't come up."""
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    env = dict(os.environ, MOCKBANK_PORT=str(port), MOCKBANK_VARIANT="corebank")
    proc = subprocess.Popen([sys.executable, "mockbank/app.py"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(40):
        if _healthz(base):
            break
        time.sleep(0.25)
    else:
        proc.terminate()
        pytest.skip("mock app did not become healthy")
    yield base
    proc.terminate()
