"""SimClient against httpx.MockTransport (no network)."""
from __future__ import annotations

import json

import httpx
import pytest

from app.errors import AppError
from app.sim_client import SimClient

STATE = {"money": 100, "meta": {"idempotency": ["k1"]}, "unknown_top_level": [1, 2]}
SNAPSHOT = {"credits": 100}


def client_for(handler, timeout: float = 3.0) -> SimClient:
    return SimClient("http://sim:8080/", timeout, transport=httpx.MockTransport(handler))


def recorder(response_json: dict, seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, json.loads(request.content) if request.content else None,
                     request.headers.get("content-type")))
        return httpx.Response(200, json=response_json)

    return handler


async def test_bootstrap_request_and_response():
    seen: list = []
    sim = client_for(recorder({"state": STATE, "snapshot": SNAPSHOT}, seen))
    player = {"id": 1, "tgId": 42, "name": "Ann", "username": "ann"}
    out = await sim.bootstrap(None, player, now=1_700_000_000_000)
    assert out == {"state": STATE, "snapshot": SNAPSHOT}
    method, path, body, ctype = seen[0]
    assert (method, path) == ("POST", "/v1/bootstrap")
    assert body == {"state": None, "now": 1_700_000_000_000, "player": player}
    assert ctype == "application/json"


async def test_command_request_and_rejection_is_not_an_error():
    seen: list = []
    reply = {"state": STATE, "result": {"ok": False, "error": "no money"}, "snapshot": SNAPSHOT}
    sim = client_for(recorder(reply, seen))
    out = await sim.command(STATE, {"type": "buildEnd"}, now=5)
    assert out["result"]["ok"] is False  # mapping to 422 command_rejected is the route's job
    assert seen[0][1] == "/v1/command" and seen[0][2] == {"state": STATE, "now": 5, "cmd": {"type": "buildEnd"}}


async def test_advance_optional_fields():
    seen: list = []
    sim = client_for(recorder({"state": STATE, "snapshot": SNAPSHOT, "advancedMs": 10}, seen))
    await sim.advance(STATE, now=7)
    await sim.advance(STATE, now=7, cap_hours=8, reason="resume")
    assert seen[0][1] == "/v1/advance" and seen[0][2] == {"state": STATE, "now": 7}
    assert seen[1][2] == {"state": STATE, "now": 7, "capHours": 8, "reason": "resume"}


async def test_snapshot_and_default_now():
    seen: list = []
    sim = client_for(recorder({"snapshot": SNAPSHOT}, seen))
    assert await sim.snapshot(STATE) == {"snapshot": SNAPSHOT}
    assert seen[0][1] == "/v1/snapshot" and seen[0][2]["now"] > 1_700_000_000_000  # ms since epoch


async def test_health_uses_get_without_body():
    seen: list = []
    sim = client_for(recorder({"status": "ok", "service": "sim"}, seen))
    assert (await sim.health())["status"] == "ok"
    assert seen[0] == ("GET", "/health", None, None)


@pytest.mark.parametrize("exc", [
    httpx.ConnectError("refused"), httpx.ConnectTimeout("t"), httpx.ReadTimeout("slow"),
    httpx.RemoteProtocolError("closed"),
])
async def test_transport_errors_become_503_sim_unavailable(exc):
    def handler(request):
        raise exc

    with pytest.raises(AppError) as e:
        await client_for(handler).command(STATE, {"type": "x"})
    assert (e.value.status, e.value.code) == (503, "sim_unavailable")
    assert e.value.details == {"reason": type(exc).__name__}


async def test_real_timeout_against_silent_server():
    """A socket that accepts and never answers: SIM_TIMEOUT_S must cut the call off quickly."""
    import asyncio
    import time

    async def silent(reader, writer):
        await asyncio.sleep(5)

    server = await asyncio.start_server(silent, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    sim = SimClient(f"http://127.0.0.1:{port}", 0.3)
    started = time.perf_counter()
    try:
        with pytest.raises(AppError) as e:
            await sim.snapshot(STATE)
    finally:
        await sim.aclose()
        server.close()
    assert (e.value.status, e.value.code) == (503, "sim_unavailable")
    assert e.value.details == {"reason": "ReadTimeout"}
    assert time.perf_counter() - started < 2


async def test_connection_refused_against_closed_port():
    sim = SimClient("http://127.0.0.1:1", 1.0)
    with pytest.raises(AppError) as e:
        await sim.health()
    assert e.value.code == "sim_unavailable"
    await sim.aclose()


@pytest.mark.parametrize("status", [500, 502, 503, 504, 404])
async def test_upstream_failures_become_503(status):
    sim = client_for(lambda r: httpx.Response(status, json={"error": {"code": "x"}}))
    with pytest.raises(AppError) as e:
        await sim.advance(STATE)
    assert (e.value.status, e.value.code) == (503, "sim_unavailable")
    assert e.value.details == {"upstream_status": status}


async def test_sim_400_means_api_bug_so_500():
    sim = client_for(lambda r: httpx.Response(400, json={"error": {"code": "bad_request", "message": "no cmd"}}))
    with pytest.raises(AppError) as e:
        await sim.command(STATE, {})
    assert (e.value.status, e.value.code) == (500, "internal_error")
    assert "no cmd" not in e.value.message  # sim internals are not echoed to the player


@pytest.mark.parametrize("content", [b"<html>", b"", b"[1, 2]", b"null"])
async def test_garbage_response_is_503(content):
    sim = client_for(lambda r: httpx.Response(200, content=content))
    with pytest.raises(AppError) as e:
        await sim.snapshot(STATE)
    assert e.value.code == "sim_unavailable"


async def test_aclose_is_idempotent_enough():
    sim = client_for(lambda r: httpx.Response(200, json={}))
    await sim.aclose()
