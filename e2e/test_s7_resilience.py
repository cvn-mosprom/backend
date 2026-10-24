"""S7 resilience against the running compose stack: restarts must not lose the factory, a dead sim must not take the API down.

Skipped unless E2E_DOCKER=1 (it runs `docker compose restart/stop/start` on the stack in the repo root, so do not run it
while somebody else is using that stack):

    E2E_DOCKER=1 .venv/Scripts/python -m pytest test_s7_resilience.py -v -s
"""

import os
import subprocess
import time
from pathlib import Path

import httpx
import pytest
from conftest import BASE, Api, err, layout_of

pytestmark = pytest.mark.skipif(os.environ.get("E2E_DOCKER") != "1", reason="set E2E_DOCKER=1 to restart containers")

ROOT = Path(__file__).resolve().parents[1]
TG_ID = 777001  # fixed on purpose: the same player before and after the restart (and across re-runs)
NAME = "S7 Resilience"


def compose(*args: str) -> None:
    subprocess.run(["docker", "compose", *args], cwd=ROOT, check=True, capture_output=True, timeout=240)


def wait_ready(timeout_s: float = 150.0) -> None:
    """Poll GET /ready (through caddy) until every dependency is back."""
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


def wait_for(predicate, timeout_s: float, what: str):
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = predicate()
            if last:
                return last
        except (httpx.HTTPError, AssertionError) as e:
            last = e
        time.sleep(1.5)
    raise AssertionError(f"timed out waiting for {what}; last: {last!r}")


def shape(snapshot: dict) -> dict:
    layout = layout_of(snapshot)
    return {"name": snapshot["factory"]["name"], "buildings": layout["buildings"], "conveyors": layout["conveyors"]}


@pytest.fixture
def player():
    a = Api(TG_ID, "s7")  # "Authorization: dev 777001:s7"
    wait_ready()
    body = a.bootstrap()
    if body["snapshot"]["factory"]["name"] != NAME:  # first run: give the factory something to lose
        a.command({"type": "dialogDone", "id": "q_intro"})
        a.command({"type": "renameFactory", "name": NAME})
        a.command({"type": "place", "kind": "brew_tank", "x": 6, "y": 1, "rot": 1})
    yield a
    a.close()


def test_restart_api_sim_db_keeps_the_factory(player: Api):
    before = player.bootstrap()
    version, layout = before["version"], shape(before["snapshot"])
    assert layout["name"] == NAME and any(b["kind"] == "brew_tank" for b in layout["buildings"])

    compose("restart", "api", "sim", "db")
    wait_ready()

    tab = player.clone()  # a fresh page load after the outage
    after = tab.bootstrap()
    assert after["version"] == version, "restart must not change the version"
    assert shape(after["snapshot"]) == layout, "same layout after restart"
    me = tab.me()
    assert me["degraded"] is False and me["version"] == version
    # and the factory is still writable with the version we saw before the restart
    player.version = version
    assert player.command({"type": "renameFactory", "name": NAME})["version"] == version + 1


def test_dead_sim_degrades_gracefully_and_recovers(player: Api):
    before = player.bootstrap()
    version, layout = before["version"], shape(before["snapshot"])
    try:
        compose("stop", "sim")
        # factory/me falls back to the stored snapshot (degraded) and keeps doing so with the breaker open
        for _ in range(14):  # > 5 failures in each of the api workers: every breaker is open afterwards
            me = player.me()
            assert me["degraded"] is True and me["version"] == version and shape(me["snapshot"]) == layout

        r = player.raw_command({"type": "renameFactory", "name": NAME})
        assert r.status_code == 503 and err(r)["code"] == "sim_unavailable"
        r = player.request("GET", "/api/bootstrap")
        assert r.status_code == 503 and err(r)["code"] == "sim_unavailable"

        # everything that does not need the sim keeps working
        assert httpx.get(f"{BASE}/health", verify=False, timeout=5.0).status_code == 200
        assert player.request("GET", "/api/season/leaderboard").status_code == 200
        ready = httpx.get(f"{BASE}/ready", verify=False, timeout=5.0)
        assert ready.status_code == 503 and ready.json()["error"]["code"] == "sim_unavailable"
    finally:
        compose("start", "sim")

    # sim back: the breaker closes after its 10 s window (+ sim boot) and the game answers normally again
    wait_for(lambda: player.me()["degraded"] is False, 90, "factory/me to leave degraded mode")
    assert player.bootstrap()["version"] == version
    wait_ready()
