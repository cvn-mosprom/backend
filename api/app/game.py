"""Factory service (docs/ARCHITECTURE.md, "S2 decisions"): every read-modify-write of `factories.state` goes through here.

Rules enforced in this module:
  * the factory row is locked with `SELECT state, version ... FOR UPDATE` for the whole sim call + write;
  * state, denormalised columns and the snapshot are written in ONE transaction;
  * `version` is bumped only by a successful player command with effect (rejected commands and every lazy catch-up
    persist the state but keep the version, so passive sync never causes a 409);
  * idempotency keys live in `state.meta.idempotency` (deque, last 50) in the same transaction as the state, with a
    Redis fast-path cache (`idem:{user_id}:{key}`, TTL 600 s) that is fail-open;
  * the sim gets `now = max(server_now_ms, state.lastTick)` (monotonic per factory);
  * `submitShift` is checked by app/shift.py under the lock BEFORE the sim (a violation is a recorded rejection: no sim
    command, no version bump, no shift consumed) and an accepted one is logged in `shifts` in the same transaction;
  * undo/redo have no special case: they go to the sim like any command (the sim serves them from `state.history`
    since S12: a normal effect command with version + 1; with nothing to undo they are an ordinary rule rejection).
"""
from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import orjson
from pydantic import BaseModel
from sqlalchemy import func, insert, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .errors import AppError
from .leaderboard import display_name, rank_of
from . import seasons
from .models import Factory, Season, Shift, User
from .shift import RULE_CODES, RULES, validate_shift
from .sim_client import SimClient, now_ms

log = logging.getLogger("mosprom.game")

OFFLINE_CAP_H = 8.0
IDEMPOTENCY_MAX = 50
IDEMPOTENCY_TTL_S = 600
FEATURES: dict[str, bool] = {"undo": False, "serverOrderEta": True,  # `undo` is replaced by the sim /health flag; the core schedules etaAt
                             "quiz": True, "leads": True}  # S8: the web client's flags (it defaults them to true)
STATE_WARN_BYTES = 150 * 1024  # spec "not more than 150 KB" (the core keeps ~10-30 KB incl. the undo diffs); bigger writes log state_bytes
# sim rejection reasons that become the top-level error code; every other reason is 422 command_rejected
TOP_LEVEL_REASONS = frozenset({"daily_limit_exceeded", "minigame_too_short", "impossible_score"})
SPEED_WINDOW_MS = 60_000  # min distance between speed baselines (see measure_speed)


# ---------------------------------------------------------------- pure helpers

def command_payload(cmd: BaseModel) -> dict[str, Any]:
    """The validated command as the sim expects it: wire names, only what the client sent, `null` of an
    optional field dropped (TS `?:`) but `null` of a required-nullable field kept (`paint.color: null` = erase)."""
    data = cmd.model_dump(by_alias=True, exclude_unset=True, mode="json")
    for name, field in type(cmd).model_fields.items():
        key = field.alias or name
        if key in data and data[key] is None and not field.is_required():
            del data[key]
    return data


def player_info(user: User) -> dict[str, Any]:
    """`player` argument of sim /v1/bootstrap (only used when the factory is created)."""
    info: dict[str, Any] = {"id": str(user.id), "tgId": user.tg_id, "name": display_name(user)}
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


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def measure_speed(snapshot: dict[str, Any], state: dict[str, Any]) -> int | None:
    """Bottles per hour for `factories.speed_bph` (leaderboard `speed` tab), or None = keep the stored value.

    The snapshot of lib/core (`snapshotOf`) does not expose the flow rate (`solveFlow().bottlesPerHour` stays inside the
    core and only feeds its own speed board), so the rate is derived:
    `snapshot.factory.bottlesPerHour` when a sim ever provides it, else the measured throughput = d(produced) / d(time)
    between a baseline kept in `state.meta.speed` ({t, p}) and now. The baseline moves only after >= 60 s, so frequent
    polling (every few seconds) does not zero the rate; the window is capped at the 8 h offline cap. Follow-up (sim):
    put `bottlesPerHour` into snapshot.factory to make this exact."""
    f = snapshot.get("factory")
    f = f if isinstance(f, dict) else {}
    if _is_num(f.get("bottlesPerHour")):
        return max(0, round(f["bottlesPerHour"]))
    produced, t = f.get("produced"), f.get("lastTick")
    if not (_is_num(produced) and _is_num(t)):
        return None
    meta = state.get("meta")
    if not isinstance(meta, dict):
        meta = state["meta"] = {}
    ref = meta.get("speed")
    if not (isinstance(ref, dict) and _is_num(ref.get("t")) and _is_num(ref.get("p"))) or produced < ref["p"] or t < ref["t"]:
        meta["speed"] = {"t": int(t), "p": produced}  # first write / new season (produced went back to 0)
        return None
    window = t - ref["t"]
    if window < SPEED_WINDOW_MS:
        return None
    meta["speed"] = {"t": int(t), "p": produced}
    return max(0, round((produced - ref["p"]) / (min(window, OFFLINE_CAP_H * 3_600_000) / 3_600_000)))


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
        # a factory that produced nothing has "quality 1" in the snapshot: it must not top the quality board
        "quality_avg": 0.0 if _is_num(f.get("produced")) and f["produced"] <= 0 else _num(f.get("quality")),
    }
    player = snapshot.get("player")
    if isinstance(player, dict) and isinstance(player.get("university"), str) and player["university"].strip():
        out["university"] = player["university"].strip()
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


def reason_code(reason: str) -> str:
    """Top-level error code of a rejection reason: the three sim reasons themselves, the submitShift rules of
    app/shift.py by their table, everything else `command_rejected`."""
    return reason if reason in TOP_LEVEL_REASONS else RULE_CODES.get(reason, "command_rejected")


def rejection(reason: str, message: str, version: int, snapshot: dict[str, Any], *, replayed: bool = False) -> AppError:
    """422 for a command refused by the game rules. Reasons daily_limit_exceeded / minigame_too_short /
    impossible_score (and every submitShift rule, e.g. duration_too_long -> impossible_score) give the top-level code,
    the rest is `command_rejected`; `details.reason` is always the precise reason."""
    code = reason_code(reason)
    details: dict[str, Any] = {"reason": reason, "message": message, "version": version, "snapshot": snapshot}
    if replayed:
        details["replayed"] = True
    # the web client applies a top-level `snapshot` (with `version` inside) next to `error`; details.snapshot stays
    return AppError(code, message, status=422, details=details, body_extra={"snapshot": {**snapshot, "version": version}})


def building_boost(state: dict[str, Any], building_id: str) -> float | None:
    """`boost.mult` of building `building_id` in the (post-command) state, None when unknown."""
    sim = state.get("sim")
    layout = sim.get("layout") if isinstance(sim, dict) else None
    buildings = layout.get("buildings") if isinstance(layout, dict) else None
    for b in buildings if isinstance(buildings, list) else []:
        if isinstance(b, dict) and b.get("id") == building_id:
            boost = b.get("boost")
            mult = boost.get("mult") if isinstance(boost, dict) else None
            return float(mult) if _num(mult, -1) >= 0 else None
    return None


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
    state: dict[str, Any]  # already aligned to the server season (seasons.align_state)
    version: int
    season_id: int  # the server season the factory belongs to NOW (may differ from the stored column: it is moved on write)
    moved: bool = False  # the state was changed by the alignment (season id / bounds): the write must refresh the snapshot
    season: Season | None = None  # the server season the state is aligned to (None for a row without a state yet)


class GameService:
    def __init__(self, sessionmaker: async_sessionmaker[AsyncSession] | None, redis: Any, sim: SimClient,
                 bots_enabled: bool = True) -> None:
        if sessionmaker is None:
            raise AppError("config_error", "DATABASE_URL is not configured")
        self.sm = sessionmaker
        self.redis = redis
        self.sim = sim
        self.bots_enabled = bots_enabled  # BOTS_ENABLED: demo bots count in snapshot.rank only when True

    # ---- db

    async def _lock(self, session: AsyncSession, user_id: int, *, skip_locked: bool = False) -> _Locked | None:
        """Row lock + season alignment. The state is rewritten to the server's newest season BEFORE any sim call
        (app/seasons.py: why the core's own season roll can then never fire). `skip_locked=True` (background catch-up):
        None when somebody else holds the row; otherwise a missing factory is 404 factory_not_found."""
        q = select(Factory.state, Factory.version, Factory.season_id).where(Factory.user_id == user_id)
        row = (await session.execute(q.with_for_update(skip_locked=skip_locked))).one_or_none()
        if row is None:
            if skip_locked:
                return None
            raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
        state, season_id, moved, season = row.state, row.season_id, False, None
        if state:  # {} = the row bootstrap has just inserted: the fresh state is aligned after the sim created it
            season = await seasons.current(session, monotonic_now(state), bots_enabled=self.bots_enabled)
            if season is not None:
                moved = seasons.align_state(state, season)
                season_id = season.id
        return _Locked(state=state, version=row.version, season_id=season_id, moved=moved, season=season)

    async def _set_rank(self, session: AsyncSession, season_id: int, user_id: int, snapshot: dict[str, Any],
                        stars: int | None = None) -> None:
        """snapshot.rank = the REAL general place / total (the sim passes no leaderboard to the core, so its snapshots carry no rank)."""
        place, total = await rank_of(session, season_id, user_id, bots_enabled=self.bots_enabled, mine=stars)
        snapshot["rank"] = {"place": place, "total": total}

    async def _persist(self, session: AsyncSession, user_id: int, season_id: int, old_state: dict[str, Any],
                       state: dict[str, Any], snapshot: dict[str, Any] | None, version: int | None = None) -> None:
        """State + snapshot + denormalised columns (+ version when a command had an effect), one UPDATE.
        `snapshot=None` (a rejection decided without a catch-up) writes the state only. The snapshot gets the real
        leaderboard rank (computed with the new stars, before the UPDATE) so the stored copy carries it too."""
        values: dict[str, Any] = {"state": keep_meta(old_state, state), "season_id": season_id, "updated_at": func.now()}
        if snapshot is not None:
            values.update(denormalise(snapshot))
            speed = measure_speed(snapshot, state)
            if speed is not None:
                values["speed_bph"] = speed
            await self._set_rank(session, season_id, user_id, snapshot, values["stars"])
            values["snapshot"] = snapshot
            old_player = old_state.get("player")
            old_uni = old_player.get("university") if isinstance(old_player, dict) else None
            if "university" in values and values["university"] != old_uni:  # setUniversity: users.university follows
                await session.execute(update(User).where(User.id == user_id).values(university=values["university"]))
            new_player = snapshot.get("player")
            if isinstance(new_player, dict) and new_player.get("ageConfirmed") is True and not (
                    isinstance(old_player, dict) and old_player.get("ageConfirmed") is True):
                # ageConfirm: the 18+ flag of the user row follows the sim state, in the same transaction
                await session.execute(update(User).where(User.id == user_id, User.age_confirmed.is_(False)).values(
                    age_confirmed=True, age_confirmed_at=func.now()))
        if version is not None:
            values["version"] = version
        size = len(orjson.dumps(values["state"]))
        if size > STATE_WARN_BYTES:  # size audit (spec: <= 150 KB)
            log.warning("factory state is over the size target", extra={"fields": {
                "state_bytes": size, "limit_bytes": STATE_WARN_BYTES, "user_id": user_id}})
        await session.execute(update(Factory).where(Factory.user_id == user_id).values(**values))

    @staticmethod
    async def _log_shift(session: AsyncSession, user_id: int, season_id: int, cmd: dict[str, Any],
                         state: dict[str, Any]) -> None:
        """One `shifts` row per ACCEPTED submitShift, in the transaction of the command (HR analytics, S6)."""
        r = cmd["result"]
        await session.execute(insert(Shift).values(
            user_id=user_id, season_id=season_id, building_id=cmd["buildingId"], profession=r["profession"],
            game_id=r["gameId"], result=r["result"], score=r["score"], duration_ms=round(r["durationMs"]),
            difficulty=r.get("difficulty"), metrics=r.get("metrics") or {},
            boost_mult=building_boost(state, cmd["buildingId"])))

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
            season = await seasons.ensure(session, wall, bots_enabled=self.bots_enabled)
            await session.execute(  # race-safe create-on-first-call; a rollback below removes the row again
                pg_insert(Factory).values(user_id=user.id, season_id=season.id, name=player["name"], state={},
                                          is_bot=user.is_bot, is_test=user.is_test)
                .on_conflict_do_nothing(index_elements=[Factory.user_id]))
            locked = await self._lock(session, user.id)
            if locked.state:
                now = monotonic_now(locked.state, wall)
                out = await self.sim.bootstrap(locked.state, player, now)  # lazy offline catch-up, 8 h cap
            else:
                now = wall
                out = await self.sim.bootstrap(None, player, now)  # fresh factory
            state, snapshot = _expect(out, "state", "snapshot")
            season = locked.season or season
            if not locked.state:  # fresh factory: the sim built it with its own season 1 (now .. now + 7 d)
                seasons.align_state(state, season)
                seasons.patch_snapshot(snapshot, season, now)
            await self._persist(session, user.id, season.id, locked.state, state, snapshot)
            season_out = {"id": season.id, "startsAt": _ms(season.starts_at), "endsAt": _ms(season.ends_at)}
        features = {**FEATURES, "undo": await self.sim.history_in_state()}  # outside the lock; cached 60 s, fail-closed
        return {"version": locked.version, "serverTime": now, "season": season_out,
                "features": features, "snapshot": snapshot}

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
                    select(Factory.state, Factory.version, Factory.season_id).where(Factory.user_id == user.id))).one_or_none()
            if row is None:
                raise AppError("factory_not_found", "Factory not found, call GET /api/bootstrap first")
            return await self._replay(user.id, row.season_id, entry, row.state, row.version)

        rejected: tuple[str, str] | None = None
        replay: tuple[dict[str, Any], dict[str, Any], int] | None = None
        async with self.sm() as session, session.begin():
            locked = await self._lock(session, user.id)
            found = find_entry(locked.state, key)
            if found is not None:  # checked BEFORE the version
                replay = (user.id, locked.season_id, found, locked.state, locked.version)
            else:
                if locked.version != version:
                    raise AppError("version_conflict", "Factory changed since your last sync, refresh and retry",
                                   details={"current_version": locked.version, "version": locked.version})
                now = monotonic_now(locked.state)
                rule = validate_shift(cmd.get("result")) if cmd.get("type") == "submitShift" else None
                if rule is not None:  # impossible shift: refused before the sim, recorded like any rejection
                    (snapshot,) = _expect(await self.sim.snapshot(locked.state, now), "snapshot")
                    await self._set_rank(session, locked.season_id, user.id, snapshot)
                    state = dict(locked.state)  # untouched but for meta.idempotency (added below)
                    result = {"ok": False, "code": rule, "error": RULES[rule][1]}
                else:
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
                await self._persist(session, user.id, locked.season_id, locked.state, state,
                                    None if rule and not locked.moved else snapshot,
                                    version=new_version if rejected is None else None)
                if rejected is None and cmd.get("type") == "submitShift":
                    await self._log_shift(session, user.id, locked.season_id, cmd, state)
        if replay is not None:
            return await self._replay(*replay)
        await self._idem_put(user.id, key, entry)  # only after the commit
        if rejected is not None:
            raise rejection(rejected[0], rejected[1], new_version, snapshot)
        return {"ok": True, "version": new_version, "result": entry["result"], "events": [],
                "snapshot": snapshot, "replayed": False}

    async def _replay(self, user_id: int, season_id: int, entry: dict[str, Any], state: dict[str, Any],
                      version: int) -> dict[str, Any]:
        """Stored short result + current version + fresh snapshot (pure sim read, nothing is written)."""
        out = await self.sim.snapshot(state, monotonic_now(state))
        (snapshot,) = _expect(out, "snapshot")
        async with self.sm() as session:
            await self._set_rank(session, season_id, user_id, snapshot)
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
            await self._persist(session, user.id, locked.season_id, locked.state, state, snapshot)
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
                await self._persist(session, user.id, locked.season_id, locked.state, state, snapshot)
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

    # ---- POST /internal/top/advance (background catch-up of the leaderboard head)

    async def background_advance(self, user_id: int) -> bool:
        """Lazy catch-up of one factory for the leaderboard (reason=background): row lock with SKIP LOCKED, one short
        transaction, NO version check and NO version bump (the player's open tab never gets a 409 from it). False =
        somebody holds the row (a player request in flight): skipped, the next run takes it."""
        async with self.sm() as session, session.begin():
            locked = await self._lock(session, user_id, skip_locked=True)
            if locked is None or not locked.state:
                return False
            out = await self.sim.advance(locked.state, monotonic_now(locked.state), OFFLINE_CAP_H, "background")
            state, snapshot = _expect(out, "state", "snapshot")
            await self._persist(session, user_id, locked.season_id, locked.state, state, snapshot)
        return True

    async def top_advance(self, limit: int = 200, budget_s: float = 240.0) -> dict[str, Any]:
        """Advance the top-`limit` real players (general tab: stars desc) of the current season one by one. A locked row
        is skipped, a sim outage stops the run (`sim_down`), any other failure of one factory is counted and the run goes on.
        -> {advanced, skipped, failed, ms, sim_down, pending, season_id}."""
        t0 = time.perf_counter()
        async with self.sm() as session:
            season = await seasons.newest(session)
            if season is None or season.closed or seasons.is_over(season, now_ms()):
                return {"advanced": 0, "skipped": 0, "failed": 0, "ms": 0, "sim_down": False, "pending": 0,
                        "season_id": season.id if season else None, "season_open": False}
            ids = list((await session.execute(
                select(Factory.user_id).where(Factory.season_id == season.id, Factory.is_bot.is_(False),
                                              Factory.is_test.is_(False))
                .order_by(Factory.stars.desc(), Factory.user_id.asc()).limit(limit))).scalars())
        advanced = skipped = failed = done = 0
        sim_down = False
        for uid in ids:
            if time.perf_counter() - t0 > budget_s:
                break
            done += 1
            try:
                if await self.background_advance(uid):
                    advanced += 1
                else:
                    skipped += 1
            except AppError as e:
                failed += 1
                log.warning("background advance failed", extra={"fields": {"user_id": uid, "code": e.code}})
                if e.code == "sim_unavailable":
                    sim_down = True
                    break
            except Exception:
                failed += 1
                log.exception("background advance crashed", extra={"fields": {"user_id": uid}})
        return {"advanced": advanced, "skipped": skipped, "failed": failed, "ms": round((time.perf_counter() - t0) * 1000),
                "sim_down": sim_down, "pending": len(ids) - done, "season_id": season.id, "season_open": True}
