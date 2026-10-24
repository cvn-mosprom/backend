"""docker compose helpers for the optional E2E_DOCKER=1 scenario (stop / start the sim container)."""

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from conftest import BASE

ROOT = Path(__file__).resolve().parents[2]
DOCKER = pytest.mark.skipif(os.environ.get("E2E_DOCKER") != "1", reason="set E2E_DOCKER=1 to stop/start the sim container")


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, timeout=240)


def wait_ready(timeout_s: float = 150.0) -> None:
    deadline = time.monotonic() + timeout_s
    last = "no answer"
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{BASE}/ready", verify=False, timeout=5.0)
            if r.status_code == 200:
                return
            last = f"{r.status_code} {r.text[:200]}"
        except httpx.HTTPError as e:
            last = type(e).__name__
        time.sleep(2)
    raise AssertionError(f"stack not ready after {timeout_s}s: {last}")


def wait_sim_healthy(timeout_s: float = 120.0) -> None:
    deadline = time.monotonic() + timeout_s
    health = ""
    while time.monotonic() < deadline:
        out = subprocess.run(["docker", "compose", "ps", "sim", "--format", "{{.Health}}"], cwd=ROOT, capture_output=True,
                             encoding="utf-8", timeout=60)
        health = out.stdout.strip()
        if health == "healthy":
            return
        time.sleep(2)
    raise AssertionError(f"sim container not healthy after {timeout_s}s: {health!r}")
