"""Thin HTTP client for the internal sim service (see docs/ARCHITECTURE.md, "Sim service HTTP API").

Every method returns the parsed JSON object of the sim response. Timeouts, connection errors and
sim 5xx raise AppError(503 sim_unavailable); a sim 400 means the API sent garbage -> 500 internal_error.
"""
from __future__ import annotations

import logging
import time
from typing import Any

import httpx
import orjson

from .errors import AppError

log = logging.getLogger("mosprom.sim")

HISTORY_TTL_S = 60.0  # how long `historyInState` of /health is trusted (features.undo)
HISTORY_TIMEOUT_S = 1.0


def now_ms() -> int:
    return int(time.time() * 1000)


def _ts(now: int | None) -> int:
    return now_ms() if now is None else now


class SimClient:
    def __init__(self, base_url: str, timeout_s: float = 3.0, transport: httpx.AsyncBaseTransport | None = None):
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
            raise AppError("sim_unavailable", "Simulation service failed",
                           details={"upstream_status": resp.status_code})
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
