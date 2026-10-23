"""In-process fake of the sim service (`/v1/*`, docs/ARCHITECTURE.md) behind httpx.MockTransport.

Not a game: just enough deterministic semantics to test the API around it.
  * state is an opaque dict; unknown top-level fields (e.g. `meta`) are round-tripped untouched, like the real sim;
  * catch-up: +1 credit and +1 shipped bottle per second since `lastTick`, capped by `capHours` / 8 h;
  * `place` costs 100 credits (`insufficient_funds` when short), `submitShift` consumes `season.shiftsLeft`
    (`daily_limit_exceeded` at 0, `minigame_too_short` below 400 ms, `impossible_score` outside 0..1 - the sim's second line
    behind the API's own checks), `acceptOrder` is rejected without `code` (the generic fallback), tick/undo/redo are
    rejected like the real sim does until `history_in_state` is switched on (then undo/redo are ordinary accepted commands
    and `/health` says `historyInState: true`); `bloat` pads the state of every command (state size guard);
  * `calls` records every request body so tests can assert what the API forwarded and with which `now`.
"""
from __future__ import annotations

import asyncio
import copy
import json
from typing import Any

import httpx

from app.sim_client import SimClient

H = 3_600_000
START_CREDITS = 1000
PLACE_COST = 100


def make_snapshot(state: dict[str, Any], now: int) -> dict[str, Any]:
    return {
        "serverTime": now,
        "factory": {"name": state["name"], "credits": state["credits"], "stars": state["stars"],
                    "level": state["level"], "xp": state["xp"], "shipped": dict(state["shipped"]),
                    "quality": 0.5, "lastTick": state["lastTick"]},
        "season": dict(state["season"]),
        "player": dict(state["player"]),
        "orders": [],
    }


class FakeSim:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []  # {"path", "body"}
        self.down = False  # connection error on every call
        self.status: int | None = None  # force this HTTP status on every call
        self.garbage: dict[str, Any] | None = None  # force this 200 body
        self.delay = 0.0  # seconds slept before answering (to make requests overlap)
        self.history_in_state = False  # S1U landed: /health flag + undo/redo accepted
        self.bloat = 0  # bytes of padding added to the state by every command
        self.health_calls = 0  # /health is not recorded in `calls` (existing tests pin the /v1 call lists)

    # ---- plumbing
    def client(self) -> SimClient:
        return SimClient("http://sim.test", 1.0, transport=httpx.MockTransport(self.handle))

    def paths(self) -> list[str]:
        return [c["path"] for c in self.calls]

    def bodies(self, path: str) -> list[dict[str, Any]]:
        return [c["body"] for c in self.calls if c["path"] == path]

    async def handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("sim is down (test)", request=request)
        if request.url.path == "/health":
            self.health_calls += 1
            return httpx.Response(200, json={"status": "ok", "service": "sim", "simCommit": "fake",
                                             "historyInState": self.history_in_state})
        body = json.loads(request.content) if request.content else {}
        self.calls.append({"path": request.url.path, "body": body})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.status is not None:
            return httpx.Response(self.status, json={"error": {"code": "forced"}})
        if self.garbage is not None:
            return httpx.Response(200, json=self.garbage)
        route = {"/v1/bootstrap": self.bootstrap, "/v1/command": self.command,
                 "/v1/advance": self.advance, "/v1/snapshot": self.snapshot}.get(request.url.path)
        if route is None:
            return httpx.Response(404, json={"error": {"code": "not_found"}})
        return httpx.Response(200, json=route(body))

    # ---- /v1/*
    @staticmethod
    def _catch_up(state: dict[str, Any], now: int, cap_h: float = 8) -> int:
        eff = max(now, state["lastTick"])
        gap = eff - state["lastTick"]
        produced = int(min(gap, cap_h * H) // 1000)
        if produced:
            state["credits"] += produced
            state["shipped"]["bottle"] = state["shipped"].get("bottle", 0) + produced
        state["lastTick"] = eff
        return gap

    def bootstrap(self, b: dict[str, Any]) -> dict[str, Any]:
        now = b["now"]
        if b["state"] is None:
            p = b["player"]
            state = {"v": 1, "name": p["name"], "player": dict(p), "lastTick": now, "credits": START_CREDITS,
                     "stars": 0, "level": 1, "xp": 0, "shipped": {}, "buildings": [],
                     "season": {"shiftsLeft": 5, "shiftsPerDay": 5}}
        else:
            state = copy.deepcopy(b["state"])
            self._catch_up(state, now)
        return {"state": state, "snapshot": make_snapshot(state, state["lastTick"])}

    def advance(self, b: dict[str, Any]) -> dict[str, Any]:
        state = copy.deepcopy(b["state"])
        gap = self._catch_up(state, b["now"], b.get("capHours", 8))
        return {"state": state, "snapshot": make_snapshot(state, state["lastTick"]), "advancedMs": gap}

    def snapshot(self, b: dict[str, Any]) -> dict[str, Any]:
        return {"snapshot": make_snapshot(b["state"], max(b["now"], b["state"]["lastTick"]))}

    def command(self, b: dict[str, Any]) -> dict[str, Any]:
        state = copy.deepcopy(b["state"])
        cmd = b["cmd"]
        t = cmd["type"]
        if self.bloat:
            state["pad"] = "x" * self.bloat
        if t in ("undo", "redo") and self.history_in_state:
            self._catch_up(state, b["now"])
            state[t] = state.get(t, 0) + 1  # a visible effect
            return self._out(state, b["now"], {"ok": True, "toast": "Отменено" if t == "undo" else "Возвращено"})
        if t in ("tick", "undo", "redo"):  # not served (real sim: REJECTED_COMMANDS)
            code = "tick_removed" if t == "tick" else "history_unavailable"
            return self._out(state, b["now"], {"ok": False, "error": code, "code": code})
        self._catch_up(state, b["now"])

        def reject(error: str, code: str | None) -> dict[str, Any]:
            res: dict[str, Any] = {"ok": False, "error": error}
            if code:
                res["code"] = code
            return self._out(state, b["now"], res)

        if t == "place":
            if state["credits"] < PLACE_COST:
                return reject("Не хватает 🪙", "insufficient_funds")
            state["credits"] -= PLACE_COST
            state["buildings"].append({"kind": cmd["kind"], "x": cmd["x"], "y": cmd["y"]})
            return self._out(state, b["now"], {"ok": True, "toast": f"Построено: {cmd['kind']}"})
        if t == "renameFactory":
            state["name"] = cmd["name"]
            return self._out(state, b["now"], {"ok": True})
        if t == "submitShift":
            if state["season"]["shiftsLeft"] <= 0:
                return reject("Смены на сегодня кончились", "daily_limit_exceeded")
            if cmd["result"]["durationMs"] < 400:
                return reject("Слишком быстро", "minigame_too_short")
            if not 0 <= cmd["result"]["score"] <= 1:
                return reject("Невозможный счёт", "impossible_score")
            state["season"]["shiftsLeft"] -= 1
            state["xp"] += 10
            r = cmd["result"]  # like the mock: the station gets a boost x(1 + 0.3 * score), halved score on a fail
            blds = state.setdefault("sim", {}).setdefault("layout", {}).setdefault("buildings", [])
            station = next((x for x in blds if x["id"] == cmd["buildingId"]), None)
            if station is None:
                blds.append(station := {"id": cmd["buildingId"]})
            station["boost"] = {"mult": round(1 + 0.3 * r["score"] * (1 if r["result"] == "win" else 0.5), 2)}
            return self._out(state, b["now"], {"ok": True, "toast": "Буст", "rewards": {"xp": 10}})
        if t == "setUniversity":  # lands in snapshot.player.university like the real sim
            state["player"]["university"] = cmd["university"]
            return self._out(state, b["now"], {"ok": True})
        if t == "acceptOrder":
            return reject("Заказ недоступен", None)  # no `code`: the API must fall back to `rejected`
        if t == "sellMarket":
            state["credits"] += int(cmd["boxes"]) * 10
            return self._out(state, b["now"], {"ok": True})
        return self._out(state, b["now"], {"ok": True})  # everything else: accepted, no visible change

    @staticmethod
    def _out(state: dict[str, Any], now: int, result: dict[str, Any]) -> dict[str, Any]:
        return {"state": state, "result": result, "snapshot": make_snapshot(state, max(now, state["lastTick"]))}
