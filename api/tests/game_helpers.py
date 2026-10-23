"""Shared helpers for the game endpoint tests (fake sim and real sim)."""
from __future__ import annotations

import json
from typing import Any

from tests.helpers import dev

SHIFT = {"difficulty": 1, "durationMs": 5000, "gameId": "g1", "metrics": {"accuracy": 0.9},
         "profession": "lab", "result": "win", "score": 0.8}


def key(n: int | str) -> str:
    return f"idem-key-{n}" if isinstance(n, str) else f"idem-key-{n:04d}"  # 8..64 chars


def place(x: int = 1, y: int = 1) -> dict[str, Any]:
    return {"type": "place", "kind": "filler", "x": x, "y": y, "rot": 0}


def shift(**result: Any) -> dict[str, Any]:
    return {"type": "submitShift", "buildingId": "b1", "result": {**SHIFT, **result}}


def js(v: Any) -> Any:
    """asyncpg returns JSONB as text."""
    return json.loads(v) if isinstance(v, (str, bytes)) else v


async def bootstrap(c, tg: int = 1, name: str | None = None) -> dict[str, Any]:
    r = await c.get("/api/bootstrap", headers=dev(tg, name))
    assert r.status_code == 200, r.text
    return r.json()


async def command(c, version: int, idem: str, cmd: dict[str, Any], tg: int = 1):
    return await c.post("/api/command", json={"version": version, "idempotency_key": idem, "cmd": cmd},
                        headers=dev(tg))


async def advance(c, body: dict[str, Any] | None = None, tg: int = 1):
    return await c.post("/api/advance", json=body, headers=dev(tg)) if body is not None else \
        await c.post("/api/advance", headers=dev(tg))


async def me(c, tg: int = 1):
    return await c.get("/api/factory/me", headers=dev(tg))


async def factory_row(c, tg: int = 1) -> dict[str, Any]:
    """The factories row of dev user `tg` with JSONB columns decoded."""
    row = await c.db.fetchrow(
        "SELECT f.* FROM factories f JOIN users u ON u.id = f.user_id WHERE u.tg_id = $1", tg)
    assert row is not None, "no factory row"
    out = dict(row)
    out["state"] = js(out["state"])
    out["snapshot"] = js(out["snapshot"])
    return out


class Clock:
    """Controllable replacement for app.game.now_ms."""

    def __init__(self, t: int) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t
