"""S10 contract test of the seam: the REAL vendor/frontend HttpGameApi (node, vitest) against the running stack.

The scenarios live in e2e/client/scenarios (journey.test.ts, retries.test.ts); this wrapper makes them part of the general run:
    cd e2e && .venv/Scripts/python -m pytest test_s10_client.py -v
It needs node + npm; `npm ci` runs once when e2e/client/node_modules is missing (skipped with a clear message when that is impossible).
Env passed through: E2E_BASE (default https://localhost; the node side accepts Caddy's self-signed cert), E2E_ORDER_WAIT_S (0 = do not wait ~55 s
for the tutorial shipment).
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from conftest import BASE

CLIENT = Path(__file__).resolve().parent / "client"
VITEST = CLIENT / "node_modules" / "vitest" / "vitest.mjs"


@pytest.fixture(scope="module")
def node() -> str:
    exe = shutil.which("node")
    if not exe:
        pytest.skip("node is not installed: the HttpGameApi contract scenarios (e2e/client) cannot run")
    if not VITEST.exists():
        npm = shutil.which("npm")
        if not npm:
            pytest.skip("e2e/client/node_modules is missing and npm is not installed: run `npm ci` in e2e/client")
        done = subprocess.run([npm, "ci", "--no-audit", "--no-fund"], cwd=CLIENT, capture_output=True, text=True, encoding="utf-8", timeout=600)
        if done.returncode != 0 or not VITEST.exists():
            pytest.skip(f"`npm ci` in e2e/client failed (offline?): {(done.stderr or done.stdout)[-400:]}")
    return exe


def test_real_http_game_api_against_the_stack(node: str):
    env = {**os.environ, "E2E_BASE": BASE, "NO_COLOR": "1", "FORCE_COLOR": "0"}
    done = subprocess.run([node, str(VITEST), "run", "--reporter=verbose"], cwd=CLIENT, env=env, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)
    out = (done.stdout or "") + (done.stderr or "")
    assert done.returncode == 0, f"HttpGameApi scenarios failed (exit {done.returncode}):\n{out[-6000:]}"
