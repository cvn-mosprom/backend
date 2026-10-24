"""S7 stabilisation: sim circuit breaker, 5 s request cap, slow-request log, DB timeouts, INTERNAL_TOKEN guard."""
from __future__ import annotations

import asyncio
import logging
import time

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.db import connect_args, make_engine
from app.errors import AppError
from app.routes import health as health_routes
from app.settings import Settings
from app.sim_client import CircuitBreaker, SimClient
from tests.game_helpers import bootstrap, command, factory_row, me, place
from tests.helpers import INTERNAL_TOKEN, TEST_DB_URL, build_settings, dev
from tests.test_errors import assert_error

STATE = {"money": 1}


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


class Upstream:
    """httpx.MockTransport handler with a switchable behaviour; counts the calls that really reach the 'network'."""

    def __init__(self) -> None:
        self.calls = 0
        self.mode = "fail"  # fail (500) | ok | timeout | connect | bad_request (400) | too_big (413) | hang
        self.gate: asyncio.Event | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        if self.mode == "timeout":
            raise httpx.ReadTimeout("slow sim", request=request)
        if self.mode == "connect":
            raise httpx.ConnectError("sim down", request=request)
        if self.mode == "hang":
            assert self.gate is not None
            await self.gate.wait()
        if self.mode == "fail":
            return httpx.Response(500, json={"error": {"code": "boom"}})
        if self.mode == "bad_request":
            return httpx.Response(400, json={"error": {"code": "bad_request"}})
        if self.mode == "too_big":
            return httpx.Response(413, json={"error": {"code": "payload_too_large"}})
        return httpx.Response(200, json={"snapshot": {"ok": True}})


def make_sim(up: Upstream, clock: FakeClock | None = None, threshold: int = 5, open_s: float = 10.0) -> SimClient:
    breaker = CircuitBreaker(threshold, open_s, clock=clock or FakeClock())
    return SimClient("http://sim.test", 1.0, transport=httpx.MockTransport(up), breaker=breaker)


async def call(sim: SimClient):
    return await sim.snapshot(STATE, now=1)


async def trip(sim: SimClient, n: int = 5) -> None:
    for _ in range(n):
        with pytest.raises(AppError):
            await call(sim)


# ================================================================ circuit breaker (unit)

async def test_breaker_opens_after_n_consecutive_failures_then_fails_fast():
    up, clock = Upstream(), FakeClock()
    sim = make_sim(up, clock)
    for i in range(4):
        with pytest.raises(AppError) as e:
            await call(sim)
        assert e.value.code == "sim_unavailable" and e.value.details["upstream_status"] == 500
        assert sim.breaker.state == "closed", i
    with pytest.raises(AppError):
        await call(sim)
    assert sim.breaker.state == "open" and up.calls == 5

    clock.t += 3
    with pytest.raises(AppError) as e:  # fail fast: the transport is not touched
        await call(sim)
    err = e.value
    assert (err.code, err.status) == ("sim_unavailable", 503)
    assert err.details["reason"] == "circuit_open" and err.details["breaker_state"] == "open"
    assert err.headers["Retry-After"] == "7" and err.details["retry_after_s"] == 7
    assert up.calls == 5
    assert sim.breaker.stats() == {"breaker_state": "open", "consecutive_failures": 5, "opened_total": 1,
                                   "rejected_total": 1}


@pytest.mark.parametrize("mode", ["timeout", "connect"])
async def test_timeouts_and_connection_errors_count_as_failures(mode):
    up = Upstream()
    up.mode = mode
    sim = make_sim(up, threshold=3)
    await trip(sim, 3)
    assert sim.breaker.state == "open"


async def test_success_resets_the_consecutive_counter():
    up = Upstream()
    sim = make_sim(up)
    await trip(sim, 4)
    up.mode = "ok"
    assert await call(sim) == {"snapshot": {"ok": True}}
    up.mode = "fail"
    await trip(sim, 4)  # 4 + 4 failures but never 5 in a row
    assert sim.breaker.state == "closed"


@pytest.mark.parametrize("mode,code", [("bad_request", "internal_error"), ("too_big", "sim_unavailable")])
async def test_sim_4xx_answers_are_not_breaker_failures(mode, code):
    """The sim is alive and answered: a bad request / an oversized state is about the caller, not an outage."""
    up = Upstream()
    up.mode = mode
    sim = make_sim(up, threshold=2)
    for _ in range(6):
        with pytest.raises(AppError) as e:
            await call(sim)
        assert e.value.code == code
    assert sim.breaker.state == "closed" and up.calls == 6


async def test_half_open_lets_exactly_one_probe_through_and_success_closes():
    up, clock = Upstream(), FakeClock()
    sim = make_sim(up, clock)
    await trip(sim)
    assert sim.breaker.state == "open"

    clock.t += 10  # open_s elapsed
    up.mode, up.gate = "hang", asyncio.Event()
    probe = asyncio.create_task(call(sim))
    await asyncio.sleep(0.05)
    assert sim.breaker.state == "half_open" and up.calls == 6
    with pytest.raises(AppError) as e:  # a second caller while the probe is in flight is rejected
        await call(sim)
    assert e.value.details["reason"] == "circuit_open" and up.calls == 6

    up.mode = "ok"
    up.gate.set()
    assert await probe == {"snapshot": {"ok": True}}
    assert sim.breaker.state == "closed"
    assert await call(sim) == {"snapshot": {"ok": True}} and up.calls == 7  # traffic flows again


async def test_failed_probe_reopens_for_another_window():
    up, clock = Upstream(), FakeClock()
    sim = make_sim(up, clock)
    await trip(sim)
    clock.t += 10
    with pytest.raises(AppError) as e:
        await call(sim)  # the probe, still failing
    assert e.value.details.get("upstream_status") == 500 and up.calls == 6
    assert sim.breaker.state == "open" and sim.breaker.opened_total == 2
    clock.t += 9
    with pytest.raises(AppError) as e:
        await call(sim)
    assert e.value.details["reason"] == "circuit_open" and up.calls == 6  # still inside the new window
    clock.t += 1
    up.mode = "ok"
    assert await call(sim) == {"snapshot": {"ok": True}} and sim.breaker.state == "closed"


async def test_cancelled_probe_does_not_wedge_the_breaker():
    up, clock = Upstream(), FakeClock()
    sim = make_sim(up, clock)
    await trip(sim)
    clock.t += 10
    up.mode, up.gate = "hang", asyncio.Event()
    probe = asyncio.create_task(call(sim))  # e.g. the 5 s request cap cancels the handler
    await asyncio.sleep(0.05)
    probe.cancel()
    with pytest.raises(asyncio.CancelledError):
        await probe
    assert sim.breaker.state == "open"  # counted as a failed probe
    clock.t += 10
    up.mode = "ok"
    assert await call(sim) == {"snapshot": {"ok": True}} and sim.breaker.state == "closed"


async def test_breaker_state_changes_are_logged_with_metrics(caplog):
    up, clock = Upstream(), FakeClock()
    sim = make_sim(up, clock)
    with caplog.at_level(logging.INFO, logger="mosprom.sim"):
        await trip(sim)
        clock.t += 10
        up.mode = "ok"
        await call(sim)
    changes = [(r.levelno, r.fields) for r in caplog.records if hasattr(r, "fields") and "breaker_state" in r.fields]
    assert [f["breaker_state"] for _, f in changes] == ["open", "half_open", "closed"]
    assert changes[0][0] == logging.WARNING and changes[0][1]["consecutive_failures"] == 5
    assert changes[0][1]["opened_total"] == 1


async def test_health_probe_goes_through_the_breaker_too():
    up = Upstream()
    sim = make_sim(up)
    await trip(sim)
    with pytest.raises(AppError) as e:
        await sim.health(timeout=0.5)
    assert e.value.details["reason"] == "circuit_open"
    assert await sim.history_in_state() is False  # undo flag fails closed, no exception


# ================================================================ circuit breaker (HTTP, real DB + fake sim)

async def test_open_breaker_serves_degraded_factory_me_and_fails_fast_elsewhere(game, fake_sim):
    await bootstrap(game)
    assert (await command(game, 0, "idem-key-0001", place())).status_code == 200
    stored = await factory_row(game)

    fake_sim.down = True
    for _ in range(5):  # five degraded answers = five consecutive sim failures
        body = (await me(game)).json()
        assert body["degraded"] is True
    breaker = game.app.state.sim.breaker
    assert breaker.state == "open"

    fake_sim.down = False  # the sim is healthy again, but the breaker keeps everyone away until the window passes
    before = len(fake_sim.calls)
    body = (await me(game)).json()
    assert body["degraded"] is True and body["snapshot"] == stored["snapshot"] and body["version"] == 1
    err = assert_error(await command(game, 1, "idem-key-0002", place(2, 2)), 503, "sim_unavailable")
    assert err["details"]["reason"] == "circuit_open"
    assert_error(await game.post("/api/advance", json={}, headers=dev(1)), 503, "sim_unavailable")
    assert_error(await game.get("/api/bootstrap", headers=dev(2)), 503, "sim_unavailable")
    assert len(fake_sim.calls) == before  # not a single request reached the sim
    assert (await factory_row(game)) == stored  # and nothing was written

    breaker.opened_at -= 11  # the open window passes: the next call is the probe
    body = (await me(game)).json()
    assert body["degraded"] is False and breaker.state == "closed"
    assert (await command(game, 1, "idem-key-0002", place(2, 2))).status_code == 200


async def test_ready_reports_circuit_open_and_stops_calling_the_sim(make_client, monkeypatch):
    async def ok(*_a, **_k) -> str:
        return "ok"

    for name in ("check_db", "check_migrations", "check_redis"):
        monkeypatch.setattr(health_routes, name, ok)
    up = Upstream()
    c = await make_client(sim=make_sim(up))
    for _ in range(5):
        assert_error(await c.get("/ready"), 503, "sim_unavailable")
    assert up.calls == 5
    err = assert_error(await c.get("/ready"), 503, "sim_unavailable")
    assert err["details"]["checks"]["sim"] == "error: circuit_open" and up.calls == 5
    assert (await c.get("/health")).status_code == 200  # liveness never depends on the sim


# ================================================================ whole-request cap and slow log

async def test_request_over_the_cap_is_503_timeout_with_retry_after(make_client, caplog):
    c = await make_client(request_timeout_s=0.2)
    with caplog.at_level(logging.INFO, logger="mosprom.access"):
        t0 = time.perf_counter()
        resp = await c.get("/t/sleep/3", headers={"X-Request-ID": "cap-1"})
        elapsed = time.perf_counter() - t0
    err = assert_error(resp, 503, "timeout")
    assert elapsed < 1.5
    assert resp.headers["retry-after"] == "1" and resp.headers["x-request-id"] == "cap-1"
    assert err["details"] == {"timeout_s": 0.2}
    line = next(r.fields for r in caplog.records if r.name == "mosprom.access" and r.fields["request_id"] == "cap-1")
    assert line["status"] == 503 and line["error_code"] == "timeout" and line["duration_ms"] >= 150
    assert (await c.get("/t/sleep/0.01")).status_code == 200  # the app keeps serving after a cancelled request


async def test_internal_paths_are_exempt_from_the_cap(make_client):
    c = await make_client(request_timeout_s=0.1)
    resp = await c.get("/internal/t/sleep/0.4")
    assert resp.status_code == 200 and resp.json() == {"slept": 0.4}


async def test_cap_can_be_disabled(make_client):
    c = await make_client(request_timeout_s=0)
    assert (await c.get("/t/sleep/0.3")).status_code == 200


async def test_slow_request_logs_a_warning_with_duration(make_client, caplog):
    c = await make_client(slow_request_ms=100, request_timeout_s=5)
    with caplog.at_level(logging.DEBUG, logger="mosprom.access"):
        await c.get("/t/sleep/0.25", headers={"X-Request-ID": "slow-1"})
        await c.get("/t/small", headers={"X-Request-ID": "fast-1"})
    lines = {r.fields["request_id"]: r for r in caplog.records if r.name == "mosprom.access"}
    slow, fast = lines["slow-1"], lines["fast-1"]
    assert slow.levelno == logging.WARNING and slow.getMessage() == "slow request"
    assert slow.fields["slow"] is True and slow.fields["duration_ms"] >= 250 and slow.fields["status"] == 200
    assert fast.levelno == logging.INFO and "slow" not in fast.fields


async def test_access_log_carries_breaker_state_only_while_not_closed(make_client, caplog):
    up = Upstream()
    sim = make_sim(up)
    c = await make_client(sim=sim)
    with caplog.at_level(logging.DEBUG, logger="mosprom.access"):
        await c.get("/t/small", headers={"X-Request-ID": "closed-1"})
        await trip(sim)
        await c.get("/t/small", headers={"X-Request-ID": "open-1"})
    lines = {r.fields["request_id"]: r.fields for r in caplog.records if r.name == "mosprom.access"}
    assert "breaker_state" not in lines["closed-1"] and lines["open-1"]["breaker_state"] == "open"


# ================================================================ DB timeouts

def test_connect_args_direct_mode_sets_session_timeouts():
    args = connect_args(build_settings(db_statement_timeout_ms=3000, db_lock_timeout_ms=2000))
    assert args["server_settings"] == {"statement_timeout": "3000", "lock_timeout": "2000"}
    assert "command_timeout" not in args and "statement_cache_size" not in args


def test_connect_args_pgbouncer_mode_avoids_startup_parameters():
    args = connect_args(build_settings(pgbouncer=True, db_statement_timeout_ms=3000))
    assert "server_settings" not in args  # pgbouncer (transaction pooling) rejects unknown startup parameters
    assert args["command_timeout"] == 3.0
    assert args["statement_cache_size"] == 0 and args["prepared_statement_cache_size"] == 0


def test_connect_args_without_timeouts_for_migrations():
    args = connect_args(build_settings(), timeouts=False)
    assert "server_settings" not in args and "command_timeout" not in args


def test_default_timeouts_follow_the_spec():
    s = Settings()
    assert (s.db_statement_timeout_ms, s.db_lock_timeout_ms, s.sim_timeout_s, s.request_timeout_s) == (3000, 2000, 3.0, 5.0)
    assert (s.slow_request_ms, s.sim_breaker_threshold, s.sim_breaker_open_s) == (1000, 5, 10.0)


@pytest.fixture
def db_routes(make_client, pg_url):
    """Client on the real test Postgres plus routes that run slow / blocked SQL through the app's sessionmaker."""

    async def _make(**overrides):
        c = await make_client(database_url=pg_url, **overrides)

        @c.app.get("/t/pg/sleep/{secs}")
        async def pg_sleep(secs: float):
            async with c.app.state.sessionmaker() as s:
                await s.execute(text("SELECT pg_sleep(:s)"), {"s": secs})
            return {"ok": True}

        @c.app.get("/t/pg/users")
        async def pg_users():
            async with c.app.state.sessionmaker() as s:
                return {"n": (await s.execute(text("SELECT count(*) FROM users"))).scalar_one()}

        @c.app.get("/t/pg/show")
        async def pg_show():
            async with c.app.state.sessionmaker() as s:
                return {k: (await s.execute(text(f"SHOW {k}"))).scalar_one() for k in ("statement_timeout", "lock_timeout")}

        return c

    return _make


async def test_statement_timeout_is_applied_to_every_connection(db_routes):
    c = await db_routes()
    assert (await c.get("/t/pg/show")).json() == {"statement_timeout": "3s", "lock_timeout": "2s"}


async def test_statement_timeout_maps_to_503_db_unavailable(db_routes):
    c = await db_routes(db_statement_timeout_ms=300, request_timeout_s=5)
    t0 = time.perf_counter()
    resp = await c.get("/t/pg/sleep/3")
    assert time.perf_counter() - t0 < 2.0
    err = assert_error(resp, 503, "db_unavailable")
    assert err["details"] == {"reason": "statement_timeout"} and resp.headers["retry-after"] == "1"
    assert (await c.get("/t/pg/sleep/0.01")).status_code == 200  # the pool recovers


async def test_pgbouncer_mode_enforces_the_same_bound_client_side(db_routes):
    c = await db_routes(pgbouncer=True, db_statement_timeout_ms=300)
    err = assert_error(await c.get("/t/pg/sleep/3"), 503, "db_unavailable")
    assert err["details"] == {"reason": "statement_timeout"}
    assert (await c.get("/t/pg/sleep/0.01")).status_code == 200


async def test_lock_timeout_maps_to_503_db_unavailable(db_routes, clean_db):
    c = await db_routes(db_lock_timeout_ms=300, db_statement_timeout_ms=5000)
    tx = clean_db.transaction()
    await tx.start()
    await clean_db.execute("LOCK TABLE users IN ACCESS EXCLUSIVE MODE")  # someone holds the table
    try:
        t0 = time.perf_counter()
        err = assert_error(await c.get("/t/pg/users"), 503, "db_unavailable")
        assert time.perf_counter() - t0 < 2.0
        assert err["details"] == {"reason": "lock_timeout"}
    finally:
        await tx.rollback()
    assert (await c.get("/t/pg/users")).status_code == 200


async def test_migration_engine_has_no_statement_timeout(pg_url):
    engine = make_engine(build_settings(), url=pg_url, timeouts=False)
    async with engine.connect() as conn:
        assert (await conn.execute(text("SHOW statement_timeout"))).scalar_one() == "0"
    await engine.dispose()


async def test_other_database_errors_stay_500(make_client):
    c = await make_client()

    class Orig(Exception):
        sqlstate = "23505"  # unique violation: a bug, not an outage

    @c.app.get("/t/dbapi")
    async def dbapi():
        raise DBAPIError("INSERT", {}, Orig("dup"))

    assert_error(await c.get("/t/dbapi"), 500, "internal_error")


@pytest.mark.parametrize("sqlstate", ["22021", "22P05"])  # invalid byte sequence (NUL) / untranslatable \u0000 in jsonb
async def test_unstorable_text_database_errors_map_to_422(make_client, sqlstate):
    c = await make_client()

    class Orig(Exception):
        pass

    Orig.sqlstate = sqlstate

    @c.app.get("/t/badtext")
    async def badtext():
        raise DBAPIError("INSERT", {}, Orig("unsupported Unicode escape sequence"))

    err = assert_error(await c.get("/t/badtext"), 422, "validation_error")
    assert "Unicode" not in err["message"]


# ================================================================ INTERNAL_TOKEN

@pytest.mark.parametrize("token,ok", [("", False), ("short", False), ("dev-internal-token", False),
                                      ("CHANGE-ME", False), ("a-long-random-secret-0123456789", True)])
def test_prod_requires_a_real_internal_token(token, ok):
    s = build_settings(app_env="prod", dev_auth=False, internal_token=token)
    assert s.internal_token_ok is ok
    assert ("INTERNAL_TOKEN" in s.missing_required()) is (not ok)


def test_dev_does_not_require_the_internal_token():
    assert "INTERNAL_TOKEN" not in build_settings(app_env="dev", internal_token="").missing_required()


async def test_prod_refuses_to_start_without_a_usable_internal_token(make_client):
    with pytest.raises(RuntimeError, match="INTERNAL_TOKEN"):
        await make_client(app_env="prod", dev_auth=False, internal_token="")
    c = await make_client(app_env="prod", dev_auth=False, internal_token=INTERNAL_TOKEN)
    assert (await c.get("/health")).status_code == 200
    await make_client(app_env="dev", internal_token="")  # dev starts without it (the internal API stays disabled)
