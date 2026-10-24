"""Thin HTTP client for the internal sim service (see docs/ARCHITECTURE.md, "Sim service HTTP API").

Every method returns the parsed JSON object of the sim response. Timeouts, connection errors and
sim 5xx raise AppError(503 sim_unavailable); a sim 400 means the API sent garbage -> 500 internal_error.

A per-process circuit breaker (S7) sits in front of every call: after `threshold` consecutive failures
(timeout / connection error / 5xx / garbage body) it opens for `open_s` seconds and every call fails fast with the same
503 sim_unavailable (details.reason = "circuit_open", `Retry-After`) without touching the network. After that exactly one
probe call is let through (half-open): success closes the breaker, failure re-opens it.
"""
from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from typing import Any

import httpx
import orjson

from .errors import AppError

log = logging.getLogger("mosprom.sim")

HISTORY_TTL_S = 60.0  # how long `historyInState` of /health is trusted (features.undo)
HISTORY_TIMEOUT_S = 1.0

CLOSED, OPEN, HALF_OPEN = "closed", "open", "half_open"


def now_ms() -> int:
    return int(time.time() * 1000)


def _ts(now: int | None) -> int:
    return now_ms() if now is None else now


class _SimAnswered(AppError):
    """503 sim_unavailable for the client, but the sim itself answered (a 4xx): not a breaker failure."""


class CircuitBreaker:
    """Consecutive-failure breaker. Single event loop per process: no locking needed (no awaits inside)."""

    def __init__(self, threshold: int = 5, open_s: float = 10.0, name: str = "sim",
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.threshold = max(1, threshold)
        self.open_s = open_s
        self.name = name
        self._clock = clock
        self.state = CLOSED
        self.failures = 0  # consecutive
        self.opened_at = 0.0
        self.probe_started: float | None = None  # a half-open probe is in flight
        self.opened_total = 0
        self.rejected_total = 0

    def retry_after_s(self) -> int:
        return max(1, math.ceil(self.open_s - (self._clock() - self.opened_at))) if self.state == OPEN else 1

    def acquire(self) -> bool:
        """Permission to call. Returns True when the call is the half-open probe (it MUST then be settled with
        success()/failure()/abandon()); raises AppError(sim_unavailable) when the call must fail fast."""
        now = self._clock()
        if self.state == OPEN and now - self.opened_at >= self.open_s:
            self._to(HALF_OPEN)
        if self.state == CLOSED:
            return False
        if self.state == HALF_OPEN and (self.probe_started is None or now - self.probe_started > self.open_s):
            self.probe_started = now  # (an abandoned probe older than open_s is replaced)
            return True
        self.rejected_total += 1
        retry = self.retry_after_s()
        raise AppError("sim_unavailable", "Simulation service is temporarily unavailable",
                       details={"reason": "circuit_open", "breaker_state": self.state, "retry_after_s": retry},
                       headers={"Retry-After": str(retry)})

    def success(self) -> None:
        self.failures = 0
        self.probe_started = None
        if self.state != CLOSED:
            self._to(CLOSED)

    def failure(self) -> None:
        self.failures += 1
        self.probe_started = None
        if self.state == HALF_OPEN or (self.state == CLOSED and self.failures >= self.threshold):
            self.opened_at = self._clock()
            self.opened_total += 1
            self._to(OPEN)

    def abandon(self) -> None:
        """The probe was cancelled before it got an answer (client gone, request timeout): count it as a failure."""
        self.failure()

    def _to(self, state: str) -> None:
        prev, self.state = self.state, state
        level = logging.WARNING if state == OPEN else logging.INFO
        log.log(level, "sim circuit breaker %s -> %s", prev, state, extra={"fields": {
            "breaker_state": state, "breaker_prev": prev, "consecutive_failures": self.failures,
            "opened_total": self.opened_total, "rejected_total": self.rejected_total, "open_s": self.open_s}})

    def stats(self) -> dict[str, Any]:
        return {"breaker_state": self.state, "consecutive_failures": self.failures,
                "opened_total": self.opened_total, "rejected_total": self.rejected_total}


class SimClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0, transport: httpx.AsyncBaseTransport | None = None,
                 breaker: CircuitBreaker | None = None, breaker_threshold: int = 5, breaker_open_s: float = 10.0):
        self.breaker = breaker or CircuitBreaker(breaker_threshold, breaker_open_s)
        self._history: tuple[float, bool] | None = None  # (monotonic expiry, historyInState)
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout_s),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, body: dict[str, Any] | None = None,
                    timeout: float | None = None) -> dict[str, Any]:
        probe = self.breaker.acquire()  # raises the fail-fast 503 while the breaker is open
        settled = False
        try:
            try:
                data = await self._request(method, path, body, timeout)
            except AppError as e:
                settled = True
                # failures of the sim itself trip the breaker; 4xx (sim answered, our request / our bug) do not
                if e.code == "sim_unavailable" and not isinstance(e, _SimAnswered):
                    self.breaker.failure()
                else:
                    self.breaker.success()
                raise
            settled = True
            self.breaker.success()
            return data
        finally:
            if probe and not settled:  # cancelled mid-flight: never leave the breaker waiting for a dead probe
                self.breaker.abandon()

    async def _request(self, method: str, path: str, body: dict[str, Any] | None,
                       timeout: float | None) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs.update(content=orjson.dumps(body), headers={"content-type": "application/json"})
        if timeout is not None:
            kwargs["timeout"] = timeout
        try:
            resp = await self._http.request(method, path, **kwargs)
        except httpx.HTTPError as e:  # timeouts, connect errors, protocol errors
            log.warning("sim %s %s failed: %s", method, path, type(e).__name__)
            raise AppError("sim_unavailable", "Simulation service is unavailable",
                           details={"reason": type(e).__name__}) from e

        if resp.status_code == 400:
            log.error("sim rejected request %s: %s", path, resp.text[:300])
            raise AppError("internal_error", "Internal server error", status=500)
        if resp.status_code != 200:
            log.warning("sim %s answered %s", path, resp.status_code)
            # 5xx = the sim is sick (counts for the breaker); other 4xx = a request problem, the sim is alive
            cls = AppError if resp.status_code >= 500 else _SimAnswered
            raise cls("sim_unavailable", "Simulation service failed", details={"upstream_status": resp.status_code})
        try:
            data = orjson.loads(resp.content)
        except orjson.JSONDecodeError as e:
            raise AppError("sim_unavailable", "Simulation service returned invalid JSON") from e
        if not isinstance(data, dict):
            raise AppError("sim_unavailable", "Simulation service returned an unexpected payload")
        return data

    async def health(self, timeout: float | None = None) -> dict[str, Any]:
        return await self._call("GET", "/health", timeout=timeout)

    async def history_in_state(self) -> bool:
        """`historyInState` of the sim /health: undo/redo are served from a history that lives in the state (S1U).
        Cached per client (= per process) for 60 s. Fail-closed: any error or a missing flag is False, errors are not cached."""
        cached = self._history
        if cached is not None and time.monotonic() < cached[0]:
            return cached[1]
        try:
            flag = (await self.health(timeout=HISTORY_TIMEOUT_S)).get("historyInState") is True
        except AppError:
            return False
        self._history = (time.monotonic() + HISTORY_TTL_S, flag)
        return flag

    async def bootstrap(self, state: dict[str, Any] | None, player: dict[str, Any],
                        now: int | None = None) -> dict[str, Any]:
        """-> {state, snapshot}. `state=None` creates a fresh factory."""
        return await self._call("POST", "/v1/bootstrap",
                                {"state": state, "now": _ts(now), "player": player})

    async def command(self, state: dict[str, Any], cmd: dict[str, Any], now: int | None = None) -> dict[str, Any]:
        """-> {state, result: {ok, error?, toast?, rewards?}, snapshot}. result.ok=false is a rule rejection."""
        return await self._call("POST", "/v1/command", {"state": state, "now": _ts(now), "cmd": cmd})

    async def advance(self, state: dict[str, Any], now: int | None = None, cap_hours: float | None = None,
                      reason: str | None = None) -> dict[str, Any]:
        """-> {state, snapshot, advancedMs}."""
        body: dict[str, Any] = {"state": state, "now": _ts(now)}
        if cap_hours is not None:
            body["capHours"] = cap_hours
        if reason is not None:
            body["reason"] = reason
        return await self._call("POST", "/v1/advance", body)

    async def snapshot(self, state: dict[str, Any], now: int | None = None) -> dict[str, Any]:
        """-> {snapshot}; pure read."""
        return await self._call("POST", "/v1/snapshot", {"state": state, "now": _ts(now)})
