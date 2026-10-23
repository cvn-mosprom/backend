"""Factory service (docs/ARCHITECTURE.md, "S2 decisions"): every read-modify-write of `factories.state` goes through here.

Rules enforced in this module:
  * the factory row is locked with `SELECT state, version ... FOR UPDATE` for the whole sim call + write;
  * state, denormalised columns and the snapshot are written in ONE transaction;
  * `version` is bumped only by a successful player command with effect (rejected commands and every lazy catch-up
    persist the state but keep the version, so passive sync never causes a 409);
  * idempotency keys live in `state.meta.idempotency` (deque, last 50) in the same transaction as the state, with a
    Redis fast-path cache (`idem:{user_id}:{key}`, TTL 600 s) that is fail-open;
  * the sim gets `now = max(server_now_ms, state.lastTick)` (monotonic per factory).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import orjson
from pydantic import BaseModel
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import AppError
from .models import Factory, Season, User
from .sim_client import SimClient, now_ms

log = logging.getLogger("mosprom.game")

SEASON_DAYS = 7  # SEASON_DAYS of vendor/frontend content/balance.ts; real rotation arrives in S4
OFFLINE_CAP_H = 8.0
IDEMPOTENCY_MAX = 50
IDEMPOTENCY_TTL_S = 600
FEATURES: dict[str, bool] = {"undo": False, "serverOrderEta": False}
# sim rejection reasons that become the top-level error code; every other reason is 422 command_rejected
TOP_LEVEL_REASONS = frozenset({"daily_limit_exceeded", "minigame_too_short", "impossible_score"})
SEASON_LOCK_KEY = 0x4D50_5301  # pg_advisory_xact_lock: serialises creation of the first open season


# ---------------------------------------------------------------- pure helpers

def command_payload(cmd: BaseModel) -> dict[str, Any]:
    """The validated command as the sim expects it: wire names (`tick.from`), only what the client sent, `null` of an
    optional field dropped (TS `?:`) but `null` of a required-nullable field kept (`paint.color: null` = erase)."""
    data = cmd.model_dump(by_alias=True, exclude_unset=True, mode="json")
    for name, field in type(cmd).model_fields.items():
        key = field.alias or name
        if key in data and data[key] is None and not field.is_required():
            del data[key]
    return data


def player_info(user: User) -> dict[str, Any]:
    """`player` argument of sim /v1/bootstrap (only used when the factory is created)."""
    name = " ".join(p for p in (user.first_name, user.last_name) if p) or user.username or f"Player {user.tg_id}"
    info: dict[str, Any] = {"id": str(user.id), "tgId": user.tg_id, "name": name}
    if user.username:
        info["username"] = user.username
    return info


def monotonic_now(state: dict[str, Any], wall_ms: int | None = None) -> int:
    """`max(server_now_ms, state.lastTick)`: time never goes back for a factory (the sim re-rolls the day on a
    backwards jump, which could re-grant shifts)."""
    wall = now_ms() if wall_ms is None else wall_ms
    last = state.get("lastTick")
    if isinstance(last, (int, float)) and not isinstance(last, bool) and math.isfinite(last):
        return max(wall, int(last))
    return wall


def _num(v: Any, default: float = 0) -> float:
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else default


def denormalise(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Columns of `factories` derived from a sim snapshot (leaderboard / analytics copies)."""
    f = snapshot.get("factory")
    f = f if isinstance(f, dict) else {}
    shipped = f.get("shipped")
    out: dict[str, Any] = {
        "credits": int(_num(f.get("credits"))),
        "stars": int(_num(f.get("stars"))),
        "level": max(1, int(_num(f.get("level"), 1))),
        "xp": int(_num(f.get("xp"))),
        "bottles_season": int(sum(_num(v) for v in shipped.values())) if isinstance(shipped, dict) else 0,
        "quality_avg": _num(f.get("quality")),
    }
    if isinstance(f.get("name"), str) and f["name"]:
        out["name"] = f["name"]
    last_tick = f.get("lastTick")
    if isinstance(last_tick, (int, float)) and not isinstance(last_tick, bool) and 0 < last_tick < 4e12:
        out["last_tick_at"] = datetime.fromtimestamp(last_tick / 1000, tz=timezone.utc)
    return out


def idempotency_entries(state: dict[str, Any]) -> list[dict[str, Any]]:
    meta = state.get("meta")
    entries = meta.get("idempotency") if isinstance(meta, dict) else None
    return [e for e in entries if isinstance(e, dict)] if isinstance(entries, list) else []


def find_entry(state: dict[str, Any], key: str) -> dict[str, Any] | None:
    for e in reversed(idempotency_entries(state)):
        if e.get("key") == key:
            return e
    return None


def with_idempotency(old_state: dict[str, Any], new_state: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    """Return `new_state` (the sim's output) with `entry` appended to `meta.idempotency` (cap 50). The deque is
    carried over from `old_state` and nothing else in the state is touched."""
    entries = (idempotency_entries(old_state) + [entry])[-IDEMPOTENCY_MAX:]
    meta = new_state.get("meta")
    if not isinstance(meta, dict):
        old_meta = old_state.get("meta")
        meta = dict(old_meta) if isinstance(old_meta, dict) else {}
    meta["idempotency"] = entries
    new_state["meta"] = meta
    return new_state


def keep_meta(old_state: dict[str, Any], new_state: dict[str, Any]) -> dict[str, Any]:
    """The sim must round-trip unknown top-level fields; if one ever drops `meta`, never lose the idempotency log."""
    if "meta" not in new_state and isinstance(old_state.get("meta"), dict):
        new_state["meta"] = old_state["meta"]
    return new_state


def rejection(reason: str, message: str, version: int, snapshot: dict[str, Any], *, replayed: bool = False) -> AppError:
    """422 for a command refused by the game rules. Reasons daily_limit_exceeded / minigame_too_short /
    impossible_score are the top-level code, the rest is `command_rejected` + details.reason."""
    code = reason if reason in TOP_LEVEL_REASONS else "command_rejected"
    details: dict[str, Any] = {"reason": reason, "message": message, "version": version, "snapshot": snapshot}
    if replayed:
        details["replayed"] = True
    return AppError(code, message, status=422, details=details)


def _ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _expect(out: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    vals = []
    for k in keys:
        v = out.get(k)
        if not isinstance(v, dict):
            log.error("sim response has no object %r", k)
            raise AppError("sim_unavailable", "Simulation service returned an unexpected payload",
                           details={"reason": f"missing_{k}"})
        vals.append(v)
    return vals


# ---------------------------------------------------------------- service

@dataclass(frozen=True)
class _Locked:
    state: dict[str, Any]
    version: int
    season_id: int


class GameService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, redis: Any, sim: SimClient) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker
        self.redis = redis
        self.sim = sim

    # ---- db

    @staticmethod
    async def _lock(session: AsyncSession, user_id: int) -> _Locked:
        row = (await session.execute(
            select(Factory.state, Factory.version, Factory.season_id)
            .where(Factory.user_id == user_id).with_for_update()
        )).one_or_none()
        if row is None:
            raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
        return _Locked(state=row.state, version=row.version, season_id=row.season_id)

    @staticmethod
    async def _persist(session: AsyncSession, user_id: int, old_state: dict[str, Any], state: dict[str, Any],
                       snapshot: dict[str, Any], version: int | None = None) -> None:
        """State + snapshot + denormalised columns (+ version when a command had an effect), one UPDATE."""
        values: dict[str, Any] = {**denormalise(snapshot), "state": keep_meta(old_state, state),
                                  "snapshot": snapshot, "updated_at": func.now()}
        if version is not None:
            values["version"] = version
        await session.execute(update(Factory).where(Factory.user_id == user_id).values(**values))

    @staticmethod
    async def _ensure_open_season(session: AsyncSession, wall_ms: int) -> int:
        """Id of the newest open season; creates season 1 when there is none (rotation is S4). Race-safe."""
        newest = select(Season.id).where(Season.closed.is_(False)).order_by(Season.id.desc()).limit(1)
        season_id = (await session.execute(newest)).scalar_one_or_none()
        if season_id is not None:
            return season_id
        await session.execute(select(func.pg_advisory_xact_lock(SEASON_LOCK_KEY)))
        season_id = (await session.execute(newest)).scalar_one_or_none()  # created while we waited?
        if season_id is None:
            starts = datetime.fromtimestamp(wall_ms / 1000, tz=timezone.utc)
            season_id = (await session.execute(
                insert(Season).values(starts_at=starts, ends_at=starts + timedelta(days=SEASON_DAYS), days=SEASON_DAYS)
                .returning(Season.id))).scalar_one()
        return season_id

    # ---- redis idempotency fast path (fail-open)

    async def _idem_get(self, user_id: int, key: str) -> dict[str, Any] | None:
        if self.redis is None:
            return None
        try:
            raw = await self.redis.get(f"idem:{user_id}:{key}")
            entry = orjson.loads(raw) if raw else None
        except Exception as e:
            log.warning("idempotency cache read failed: %s", type(e).__name__)
            return None
        return entry if isinstance(entry, dict) and entry.get("key") == key else None

    async def _idem_put(self, user_id: int, key: str, entry: dict[str, Any]) -> None:
        if self.redis is None:
            return
        try:
            await self.redis.set(f"idem:{user_id}:{key}", orjson.dumps(entry).decode(), ex=IDEMPOTENCY_TTL_S)
        except Exception as e:
            log.warning("idempotency cache write failed: %s", type(e).__name__)

    # ---- GET /api/bootstrap

    async def bootstrap(self, user: User) -> dict[str, Any]:
        player = player_info(user)
        wall = now_ms()
        async with self.sm() as session, session.begin():
            season_id = await self._ensure_open_season(session, wall)
            await session.execute(  # race-safe create-on-first-call; a rollback below removes the row again
                pg_insert(Factory).values(user_id=user.id, season_id=season_id, name=player["name"], state={})
                .on_conflict_do_nothing(index_elements=[Factory.user_id]))
            locked = await self._lock(session, user.id)
            if locked.state:
                now = monotonic_now(locked.state, wall)
                out = await self.sim.bootstrap(locked.state, player, now)  # lazy offline catch-up, 8 h cap
            else:
                now = wall
                out = await self.sim.bootstrap(None, player, now)  # fresh factory
            state, snapshot = _expect(out, "state", "snapshot")
            await self._persist(session, user.id, locked.state, state, snapshot)
            season = (await session.execute(select(Season).where(Season.id == locked.season_id))).scalar_one()
            season_out = {"id": season.id, "startsAt": _ms(season.starts_at), "endsAt": _ms(season.ends_at)}
        return {"version": locked.version, "serverTime": now, "season": season_out,
                "features": dict(FEATURES), "snapshot": snapshot}

    # ---- POST /api/command

    async def build_end(self, user: User) -> dict[str, Any]:
        """No-op command: current version without a lock, no write, no idempotency record, no sim call."""
        async with self.sm() as session:
            version = (await session.execute(select(Factory.version).where(Factory.user_id == user.id))).scalar_one_or_none()
        if version is None:
            raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
        return {"ok": True, "version": version, "result": {}, "events": [], "snapshot": None, "replayed": False}

    async def command(self, user: User, version: int, key: str, cmd: dict[str, Any]) -> dict[str, Any]:
        """Command with effect. Returns the 200 body; raises AppError (404, 409, 422, 503)."""
        entry = await self._idem_get(user.id, key)
        if entry is not None:  # replay seen in Redis: no lock, no write
            async with self.sm() as session:
                row = (await session.execute(
                    select(Factory.state, Factory.version).where(Factory.user_id == user.id))).one_or_none()
            if row is None:
                raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
            return await self._replay(entry, row.state, row.version)

        rejected: tuple[str, str] | None = None
        replay: tuple[dict[str, Any], dict[str, Any], int] | None = None
        async with self.sm() as session, session.begin():
            locked = await self._lock(session, user.id)
            found = find_entry(locked.state, key)
            if found is not None:  # checked BEFORE the version
                replay = (found, locked.state, locked.version)
            else:
                if locked.version != version:
                    raise AppError("version_conflict", "Factory changed since your last sync, refresh and retry",
                                   details={"current_version": locked.version})
                now = monotonic_now(locked.state)
                out = await self.sim.command(locked.state, cmd, now)
                state, result, snapshot = _expect(out, "state", "result", "snapshot")
                if result.get("ok") is True:
                    new_version = locked.version + 1
                    short = {k: result[k] for k in ("toast", "rewards") if result.get(k) is not None}
                    entry = {"key": key, "ok": True, "version": new_version, "result": short, "at": now}
                else:
                    new_version = locked.version  # catch-up part is persisted, the version is not bumped
                    reason = result.get("code")
                    reason = reason if isinstance(reason, str) and reason else "rejected"
                    message = result.get("error")
                    message = message if isinstance(message, str) and message else "Command rejected"
                    rejected = (reason, message)
                    entry = {"key": key, "ok": False, "code": reason, "version": new_version,
                             "result": {"message": message}, "at": now}
                state = with_idempotency(locked.state, state, entry)
                await self._persist(session, user.id, locked.state, state, snapshot,
                                    version=new_version if rejected is None else None)
        if replay is not None:
            return await self._replay(*replay)
        await self._idem_put(user.id, key, entry)  # only after the commit
        if rejected is not None:
            raise rejection(rejected[0], rejected[1], new_version, snapshot)
        return {"ok": True, "version": new_version, "result": entry["result"], "events": [],
                "snapshot": snapshot, "replayed": False}

    async def _replay(self, entry: dict[str, Any], state: dict[str, Any], version: int) -> dict[str, Any]:
        """Stored short result + current version + fresh snapshot (pure sim read, nothing is written)."""
        out = await self.sim.snapshot(state, monotonic_now(state))
        (snapshot,) = _expect(out, "snapshot")
        result = entry.get("result") if isinstance(entry.get("result"), dict) else {}
        if entry.get("ok") is False:
            reason = entry.get("code") if isinstance(entry.get("code"), str) else "rejected"
            message = result.get("message") if isinstance(result.get("message"), str) else "Command rejected"
            raise rejection(reason, message, version, snapshot, replayed=True)
        return {"ok": True, "version": version, "result": result, "events": [], "snapshot": snapshot, "replayed": True}

    # ---- POST /api/advance

    async def advance(self, user: User, cap_hours: float, reason: str) -> dict[str, Any]:
        async with self.sm() as session, session.begin():
            locked = await self._lock(session, user.id)
            now = monotonic_now(locked.state)
            out = await self.sim.advance(locked.state, now, cap_hours, reason)
            state, snapshot = _expect(out, "state", "snapshot")
            await self._persist(session, user.id, locked.state, state, snapshot)
        advanced = out.get("advancedMs")
        return {"version": locked.version, "serverTime": now,
                "advancedMs": int(_num(advanced)), "snapshot": snapshot}

    # ---- GET /api/factory/me

    async def factory_me(self, user: User) -> dict[str, Any]:
        try:
            async with self.sm() as session, session.begin():
                locked = await self._lock(session, user.id)
                now = monotonic_now(locked.state)
                out = await self.sim.advance(locked.state, now, OFFLINE_CAP_H, "resume")
                state, snapshot = _expect(out, "state", "snapshot")
                await self._persist(session, user.id, locked.state, state, snapshot)
            return {"version": locked.version, "serverTime": now, "snapshot": snapshot, "degraded": False}
        except AppError as e:
            if e.code != "sim_unavailable":
                raise
            log.warning("factory/me degraded for user %s: %s", user.id, e.details.get("reason") or e.message)
        # degraded: the last stored snapshot, no catch-up, no write
        async with self.sm() as session:
            row = (await session.execute(
                select(Factory.version, Factory.snapshot).where(Factory.user_id == user.id))).one_or_none()
        if row is None:
            raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
        if row.snapshot is None:
            raise AppError("sim_unavailable", "Simulation service is unavailable and no snapshot is stored yet")
        return {"version": row.version, "serverTime": now_ms(), "snapshot": row.snapshot, "degraded": True}
