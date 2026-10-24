"""S4 part B against the running stack: /internal/* is not reachable through Caddy; inside the stack (`docker compose exec api`,
run from the repo root; the tests skip when docker is not usable) the token-protected endpoints of the bot's jobs work:
season rotation (idempotent), top-N background catch-up (never bumps the version), bot growth."""

import json
import subprocess
import textwrap
from pathlib import Path

import httpx
import pytest

from conftest import BASE, Api

ROOT = Path(__file__).resolve().parents[1]
PATHS = ["/internal/seasons/rotate", "/internal/top/advance", "/internal/bots/grow"]

CALL = textwrap.dedent("""
    import json, os, sys, urllib.error, urllib.request
    path, use_token, body = sys.argv[1], sys.argv[2], sys.argv[3]
    headers = {"Content-Type": "application/json"}
    if use_token == "1":
        headers["X-Internal-Token"] = os.environ["INTERNAL_TOKEN"]
    elif use_token:
        headers["X-Internal-Token"] = use_token
    req = urllib.request.Request("http://127.0.0.1:8000" + path, data=body.encode(), headers=headers, method="POST")
    try:
        r = urllib.request.urlopen(req, timeout=300)
        status, raw = r.status, r.read()
    except urllib.error.HTTPError as e:
        status, raw = e.code, e.read()
    print(json.dumps({"status": status, "body": json.loads(raw or b"null")}))
""")


def internal(path: str, token: str = "1", body: dict | None = None) -> tuple[int, dict]:
    """POST from inside the api container. token: "1" = the container's own INTERNAL_TOKEN, "" = none, else literal."""
    try:
        out = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-c", CALL, path, token,
                              json.dumps(body or {})], cwd=ROOT, capture_output=True, text=True, timeout=330)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"cannot exec into the api container: {e}")
    if out.returncode != 0:
        pytest.skip(f"docker compose exec failed: {out.stderr[-300:]}")
    res = json.loads(out.stdout.strip().splitlines()[-1])
    return res["status"], res["body"]


@pytest.fixture(scope="module", autouse=True)
def bots():
    try:
        out = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-m", "app.seed_bots"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"cannot run the bot seed: {e}")
    if out.returncode != 0:
        pytest.skip(f"bot seed failed: {out.stderr[-300:]}")


@pytest.mark.parametrize("path", PATHS)
def test_internal_endpoints_are_not_exposed_by_caddy(path):
    with httpx.Client(base_url=BASE, verify=False, timeout=15) as http:
        for headers in ({}, {"X-Internal-Token": "dev-internal-token"}):
            r = http.post(path, headers=headers)
            assert r.status_code == 404, (path, r.status_code, r.text)
            assert "error" not in r.text  # caddy's own 404, the api never saw it
        assert http.get(path).status_code == 404


@pytest.mark.parametrize("path", PATHS)
def test_internal_endpoints_need_the_token(path):
    for token in ("", "wrong-token"):
        status, body = internal(path, token)
        assert status == 401 and body["error"]["code"] == "unauthorized"


def test_rotate_is_idempotent_and_reports_ids():
    status, first = internal(PATHS[0])
    assert status == 200 and set(first) == {"closed", "opened"}
    assert all(isinstance(i, int) for i in first["closed"]) and first["opened"] in (None, *range(1, 10**6))
    status, again = internal(PATHS[0])
    assert status == 200 and again == {"closed": [], "opened": None}


def test_top_advance_never_bumps_the_version_or_409s(api: Api):
    boot = api.bootstrap()
    assert boot["version"] == 0
    status, out = internal(PATHS[1], body={"limit": 1000})
    assert status == 200, out
    assert out["failed"] == 0 and out["sim_down"] is False and out["advanced"] >= 1 and out["ms"] >= 0
    assert {"advanced", "skipped", "failed", "ms"} <= set(out)
    assert api.me()["version"] == 0  # background catch-up: no version bump ...
    r = api.raw_command({"type": "dialogDone", "id": "intro"}, version=0)  # ... so the tab that last saw 0 is not rejected
    assert r.status_code == 200, r.text
    assert r.json()["version"] == 1


def test_bots_grow_reports_progress():
    status, out = internal(PATHS[2])
    assert status == 200 and {"bots", "season_id", "progress"} <= set(out)
    assert out["bots"] >= 30  # seeded above
