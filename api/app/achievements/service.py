"""Achievements service (C1): gathers the data bundle, runs the pure engine, writes what is new.

Entry points
  on_command(session, user, season_id, state, shift=...)  the hook inside the game write transaction (GameService.command):
        `shift=False` -> cheap, state-only rules; `shift=True` (after an accepted submitShift) -> also the shift rules.
        Runs in a SAVEPOINT: a bug or a DB error here is logged and rolled back to the savepoint, the command itself
        is never lost because of it. Bots and test users are skipped.
  recompute(sessionmaker, ...)     full evaluation (all rules) of players changed since their last full evaluation, in batches,
        for POST /internal/achievements/recompute (bot job every 10 min) and the backfill command.

Rules of the write side
  * earned rows are append-only: INSERT .. ON CONFLICT DO NOTHING on (user_id, achievement_id, season_key); only rows the
    insert really created are reported as unlocked (a concurrent evaluation cannot double-report);
  * nothing is ever deleted or lowered: progress is upserted with GREATEST, points are re-summed from the earned rows;
  * the engine's counters (orders seen, loans repaid) live in achievement_progress `_counters`.meta; the progress rows of a
    player are read FOR UPDATE, so the hook (holding the factory lock) and the recompute never lose each other's counter updates.

C2 on top (same transaction as the grant): lead_events `achievement` + outbox `achievement_unlocked` for every row really created
(`_announce`; a backfill writes no outbox), and player_scores.competency / fit (`scores.py`) on every full evaluation, after a shift
and when the cheap hook unlocked something (`_score_players`).
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy import BigInteger, Integer, text
from sqlalchemy.dialects.postgresql import ARRAY, insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .. import quiz_bank
from ..models import (AchievementEarned, AchievementProgress, Factory, LeadEvent, Outbox, PlayerScore, QuizAnswer, SeasonResult,
                      User)
from . import catalog as cat
from . import scores as scoring
from .engine import evaluate, professions

log = logging.getLogger("mosprom.achievements")

VIA_HOOK, VIA_SHIFT, VIA_RECOMPUTE, VIA_BACKFILL = "hook", "shift", "recompute", "backfill"
_EARNED_KEY = ["user_id", "achievement_id", "season_key"]


class _DryRun(Exception):
    """Raised inside the recompute transaction to roll it back (`dry_run`)."""


def eligible(user: User) -> bool:
    """Demo bots and e2e / test players never earn achievements (S13: they are outside every board and analytic)."""
    return not (user.is_bot or user.is_test)


def view(a: cat.Achievement) -> dict[str, Any]:
    """An unlocked achievement as the command response will carry it (C2: `achievementsUnlocked`)."""
    return {"id": a.id, "title": a.title, "description": a.description, "category": a.category, "tier": a.tier,
            "points": a.points, "hidden": a.hidden, "seasonal": a.seasonal}


def _chunks(rows: list[Any], size: int) -> list[list[Any]]:
    return [rows[i:i + size] for i in range(0, len(rows), size)]


def _any(col: Any, ids: list[int]) -> Any:
    return col == sa.any_(sa.literal(ids, type_=ARRAY(BigInteger)))


# ---------------------------------------------------------------- data bundle

async def gather_aggregates(session: AsyncSession, user_ids: list[int], *, shifts: bool = True, season: bool = True,
                            quiz: bool = True, team: bool = True) -> dict[int, dict[str, Any]]:
    """Aggregates of the other tables for a batch of players (grouped queries, never one per player). A source that was not
    asked for is absent from the player's dict, which the engine reads as "do not evaluate those rules"."""
    out: dict[int, dict[str, Any]] = {uid: {} for uid in user_ids}
    if not user_ids:
        return out
    ids = sa.bindparam("ids", user_ids, type_=ARRAY(BigInteger))
    if shifts:
        for uid in user_ids:
            out[uid]["shifts"] = {"by_profession": {}, "fast_reaction": 0, "perfect": 0, "days": {}, "by_season": {}}
        rows = await session.execute(text(
            "SELECT user_id, profession, count(*) AS played, count(*) FILTER (WHERE result = 'win') AS wins, "
            "avg(score)::float8 AS avg_score, "
            "count(*) FILTER (WHERE (CASE WHEN jsonb_typeof(metrics->'reaction_ms') = 'number' "
            "THEN (metrics->>'reaction_ms')::float8 END) < :fast_reaction_ms) AS fast, "
            "count(*) FILTER (WHERE result = 'win' AND score >= :perfect_score) AS perfect "
            "FROM shifts WHERE user_id = ANY(:ids) GROUP BY user_id, profession").bindparams(
                ids, sa.bindparam("fast_reaction_ms", cat.FAST_REACTION_MS, type_=sa.Float),
                sa.bindparam("perfect_score", cat.PERFECT_SCORE, type_=sa.REAL)))
        for r in rows:
            s = out[r.user_id]["shifts"]
            s["by_profession"][r.profession] = {"played": r.played, "wins": r.wins, "avg_score": r.avg_score}
            s["fast_reaction"] += r.fast
            s["perfect"] += r.perfect
        rows = await session.execute(text(
            "SELECT user_id, season_id, (created_at AT TIME ZONE 'Europe/Moscow')::date AS day, count(*) AS n "
            "FROM shifts WHERE user_id = ANY(:ids) GROUP BY user_id, season_id, day").bindparams(ids))
        for r in rows:
            s = out[r.user_id]["shifts"]
            s["days"][r.day] = s["days"].get(r.day, 0) + r.n
            s["by_season"][r.season_id] = s["by_season"].get(r.season_id, 0) + r.n
        for uid in user_ids:  # the engine reads days as [[iso day, n]]
            s = out[uid]["shifts"]
            s["days"] = [[d.isoformat() if isinstance(d, date) else str(d), n] for d, n in sorted(s["days"].items())]
    if season:
        for uid in user_ids:
            out[uid]["season_results"] = []
        rows = await session.execute(text(
            "SELECT user_id, season_id, place FROM season_results WHERE user_id = ANY(:ids) AND NOT is_bot").bindparams(ids))
        for r in rows:
            out[r.user_id]["season_results"].append({"season_id": r.season_id, "place": r.place})
    if quiz:
        for uid in user_ids:
            out[uid]["quiz"] = {"correct": 0, "answered": 0}
        rows = await session.execute(text(
            "SELECT user_id, count(*) FILTER (WHERE correct) AS correct, count(*) AS answered FROM quiz_answers "
            "WHERE user_id = ANY(:ids) AND question_id = ANY(:qids) GROUP BY user_id").bindparams(
            ids, sa.bindparam("qids", list(quiz_bank.IDS), type_=ARRAY(sa.Text))))
        for r in rows:
            out[r.user_id]["quiz"] = {"correct": r.correct, "answered": r.answered}
    if team:
        # `ref_<player id>` is the start parameter of the invite link the client builds (telegram.ts refLink); the backend never
        # writes factories.state.referrals, so the invited players are counted from users.source
        for uid in user_ids:
            out[uid]["referrals"] = 0
        rows = await session.execute(text(
            "SELECT source, count(*) AS n FROM users WHERE source = ANY(:srcs) AND NOT is_bot AND NOT is_test "
            "AND source <> 'ref_' || id::text GROUP BY source").bindparams(
            sa.bindparam("srcs", [f"ref_{uid}" for uid in user_ids], type_=ARRAY(sa.Text))))
        for r in rows:
            out[int(r.source[4:])]["referrals"] = r.n
    return out


# ---------------------------------------------------------------- previous state of a batch of players

@dataclass
class Prev:
    earned: set[tuple[int, str, int]] = field(default_factory=set)
    progress: dict[tuple[int, str, int], tuple[float, float]] = field(default_factory=dict)  # -> (current, target)
    counters: dict[int, dict[str, Any]] = field(default_factory=dict)


async def load_previous(session: AsyncSession, user_ids: list[int]) -> Prev:
    """Earned set, progress and counters of the players. The progress rows are locked (ordered, so two batches cannot
    deadlock): from here on nobody else updates this player's counters until the transaction ends."""
    prev = Prev()
    if not user_ids:
        return prev
    # ONE round trip: the progress rows are locked in a CTE (FOR UPDATE is not allowed on the UNION itself)
    rows = await session.execute(text(
        "WITH p AS (SELECT user_id, achievement_id, season_key, current, target, meta FROM achievement_progress "
        "WHERE user_id = ANY(:ids) ORDER BY user_id, achievement_id, season_key FOR UPDATE) "
        "SELECT 'p' AS k, user_id, achievement_id, season_key, current, target, meta::text AS meta FROM p "
        "UNION ALL SELECT 'e', user_id, achievement_id, season_key, NULL, NULL, NULL FROM achievements_earned "
        "WHERE user_id = ANY(:ids)").bindparams(sa.bindparam("ids", sorted(user_ids), type_=ARRAY(BigInteger))))
    for r in rows:
        if r.k == "e":
            prev.earned.add((r.user_id, r.achievement_id, r.season_key))
        elif r.achievement_id == cat.COUNTERS_ID:
            meta = json.loads(r.meta) if r.meta else {}
            prev.counters[r.user_id] = meta if isinstance(meta, dict) else {}
        else:
            prev.progress[(r.user_id, r.achievement_id, r.season_key)] = (r.current, r.target)
    return prev


# ---------------------------------------------------------------- evaluate + write

@dataclass
class Item:
    user_id: int
    season_id: int
    state: dict[str, Any]
    aggregates: dict[str, Any] = field(default_factory=dict)


def compute(item: Item, prev: Prev, *, cheap: bool) -> dict[str, Any]:
    bundle = {"state": item.state, "season_id": item.season_id, "counters": prev.counters.get(item.user_id), **item.aggregates}
    return evaluate(bundle, cheap=cheap)


async def write_results(session: AsyncSession, items: list[Item], results: dict[int, dict[str, Any]], prev: Prev, *, via: str,
                        evaluated_at: datetime | None = None) -> dict[int, list[dict[str, Any]]]:
    """Persist the engine results of a batch. Returns {user_id: [newly unlocked, catalog order]}. `evaluated_at` marks a FULL
    evaluation (player_scores.evaluated_at, used by the recompute to find players that changed since)."""
    season_of = {i.user_id: i.season_id for i in items}
    new_rows: list[dict[str, Any]] = []
    progress_rows: list[dict[str, Any]] = []
    for uid, res in results.items():
        for e in res["earned"]:
            if (uid, e["id"], e["season_key"]) not in prev.earned:
                new_rows.append({"user_id": uid, "achievement_id": e["id"], "season_key": e["season_key"],
                                 "earned_season_id": season_of[uid], "tier": e["tier"], "points": e["points"],
                                 "evidence": e["evidence"], "catalog_version": res["version"], "granted_via": via})
        for p in res["progress"]:
            old = prev.progress.get((uid, p["id"], p["season_key"]))
            if old is None or p["current"] > old[0] + 1e-9 or p["target"] != old[1]:
                progress_rows.append({"user_id": uid, "achievement_id": p["id"], "season_key": p["season_key"],
                                      "current": p["current"], "target": p["target"], "meta": {}})
        if res["counters"] != prev.counters.get(uid):
            progress_rows.append({"user_id": uid, "achievement_id": cat.COUNTERS_ID, "season_key": 0, "current": 0,
                                  "target": 0, "meta": res["counters"]})
    unlocked: dict[int, list[dict[str, Any]]] = {}
    created: set[tuple[int, str, int]] = set()
    for chunk in _chunks(new_rows, 1000):  # asyncpg allows 32767 bind parameters per statement
        got = await session.execute(pg_insert(AchievementEarned).values(chunk)
                                    .on_conflict_do_nothing(index_elements=_EARNED_KEY)
                                    .returning(AchievementEarned.user_id, AchievementEarned.achievement_id,
                                               AchievementEarned.season_key))
        created.update((r.user_id, r.achievement_id, r.season_key) for r in got)
    if new_rows:
        order = {a.id: n for n, a in enumerate(cat.CATALOG)}
        for row in sorted(new_rows, key=lambda r: (r["user_id"], order[r["achievement_id"]], r["season_key"])):
            if (row["user_id"], row["achievement_id"], row["season_key"]) in created:
                unlocked.setdefault(row["user_id"], []).append(view(cat.BY_ID[row["achievement_id"]]))
    t = AchievementProgress.__table__
    for chunk in _chunks(progress_rows, 2000):
        ins = pg_insert(AchievementProgress).values(chunk)
        await session.execute(ins.on_conflict_do_update(
            index_elements=_EARNED_KEY, set_={
                "current": sa.func.greatest(t.c.current, ins.excluded.current), "target": ins.excluded.target,
                "meta": ins.excluded.meta, "updated_at": sa.func.now()}))
    if created:
        await _announce(session, [r for r in new_rows if (r["user_id"], r["achievement_id"], r["season_key"]) in created], via)
    full = evaluated_at is not None
    scored = await _score_players(session, items, results, prev, set(unlocked), full)
    touched = sorted(season_of) if full else sorted(set(unlocked) | set(scored))
    if touched:
        await _upsert_scores(session, [(uid, season_of[uid]) for uid in touched], evaluated_at, scored)
    return unlocked


async def _announce(session: AsyncSession, rows: list[dict[str, Any]], via: str) -> None:
    """Tell the world about achievements this transaction really created (same transaction, so a rollback takes them back):
      * lead_events `achievement` {id, title, points, tier} on every active lead of the player (the candidate history in the CRM);
      * outbox `achievement_unlocked` for the bot's player notifications (the bot filters: gold / seasonal, opted in). A backfill
        creates no outbox rows: a one-off run must not message everybody about old achievements."""
    rows = sorted(rows, key=lambda r: (r["user_id"], cat.CATALOG.index(cat.BY_ID[r["achievement_id"]]), r["season_key"]))
    uids = sorted({r["user_id"] for r in rows})
    leads: dict[int, list[int]] = {}
    for r in await session.execute(text(
            "SELECT id, user_id FROM leads WHERE user_id = ANY(:ids) AND consent_revoked_at IS NULL ORDER BY id").bindparams(
            sa.bindparam("ids", uids, type_=ARRAY(BigInteger)))):
        leads.setdefault(r.user_id, []).append(r.id)
    events: list[dict[str, Any]] = []
    outbox: list[dict[str, Any]] = []
    for r in rows:
        a = cat.BY_ID[r["achievement_id"]]
        for lead_id in leads.get(r["user_id"], []):
            events.append({"lead_id": lead_id, "kind": "achievement",
                           "payload": {"id": a.id, "title": a.title, "points": a.points, "tier": a.tier}})
        if via != VIA_BACKFILL:
            outbox.append({"kind": "achievement_unlocked", "payload": {
                "user_id": r["user_id"], "achievement_id": a.id, "title": a.title, "tier": a.tier, "points": a.points,
                "seasonal": a.seasonal, "season_id": r["earned_season_id"], "via": via}})
    for chunk in _chunks(events, 2000):
        await session.execute(pg_insert(LeadEvent).values(chunk))
    for chunk in _chunks(outbox, 2000):
        await session.execute(pg_insert(Outbox).values(chunk))


async def _score_players(session: AsyncSession, items: list[Item], results: dict[int, dict[str, Any]], prev: Prev,
                         unlocked: set[int], full: bool) -> dict[int, tuple[dict[str, int], dict[str, Any]]]:
    """(competency, fit) per player to store. Always on a full evaluation and after a shift (the skills moved); in the cheap
    hook only for players that just unlocked something (the proof of their competencies changed). Inputs: the state's skills, the
    per-profession shift stats (merged with the `shifts` table), every earned id and the best known progress of the others."""
    by_uid = {i.user_id: i for i in items}
    want = [u for u in results if u in by_uid and (full or u in unlocked or by_uid[u].aggregates.get("shifts") is not None)]
    if not want:
        return {}
    earned: dict[int, set[str]] = {u: set() for u in want}
    progress: dict[int, dict[str, float]] = {u: {} for u in want}
    for uid, aid, _ in prev.earned:
        if uid in earned:
            earned[uid].add(aid)
    for (uid, aid, _), (cur, _t) in prev.progress.items():
        if uid in progress:
            progress[uid][aid] = max(progress[uid].get(aid, 0.0), cur)
    extra = [u for u in want if by_uid[u].aggregates.get("shifts") is None]  # cheap hook + unlock: one grouped query
    table = (await gather_aggregates(session, extra, season=False, quiz=False, team=False)) if extra else {}
    out: dict[int, tuple[dict[str, int], dict[str, Any]]] = {}
    for uid in want:
        res = results[uid]
        earned[uid].update(e["id"] for e in res["earned"])
        for p in res["progress"]:
            progress[uid][p["id"]] = max(progress[uid].get(p["id"], 0.0), p["current"])
        by_prof = res["by_profession"] if uid not in table else professions(by_uid[uid].state, table[uid]["shifts"]["by_profession"])
        out[uid] = scoring.score_player(by_uid[uid].state, by_prof, earned[uid], progress[uid])
    return out


async def _upsert_scores(session: AsyncSession, pairs: list[tuple[int, int]], evaluated_at: datetime | None,
                         scored: dict[int, tuple[dict[str, int], dict[str, Any]]]) -> None:
    """player_scores rows: lifetime points and the part earned in the row's season, re-summed from the earned rows (so the
    figure is always consistent with them, however often this runs); competency / fit as just scored (a player not scored in
    this call keeps what the row has)."""
    comp = [json.dumps(scored[u][0]) if u in scored else None for u, _ in pairs]
    fit = [json.dumps(scored[u][1], ensure_ascii=False) if u in scored else None for u, _ in pairs]
    await session.execute(text(
        "INSERT INTO player_scores (user_id, season_id, achievement_points, season_points, competency, fit, evaluated_at, updated_at) "
        "SELECT t.uid, t.sid, "
        "COALESCE((SELECT sum(e.points) FROM achievements_earned e WHERE e.user_id = t.uid), 0), "
        "COALESCE((SELECT sum(e.points) FROM achievements_earned e WHERE e.user_id = t.uid AND e.earned_season_id = t.sid), 0), "
        "COALESCE(CAST(t.comp AS jsonb), '{}'::jsonb), COALESCE(CAST(t.fit AS jsonb), '{}'::jsonb), :ev, now() "
        "FROM unnest(:uids, :sids, :comp, :fit) AS t(uid, sid, comp, fit) "
        "ON CONFLICT (user_id, season_id) DO UPDATE SET achievement_points = EXCLUDED.achievement_points, "
        "season_points = EXCLUDED.season_points, updated_at = now(), "
        "competency = CASE WHEN EXCLUDED.competency = '{}'::jsonb THEN player_scores.competency ELSE EXCLUDED.competency END, "
        "fit = CASE WHEN EXCLUDED.fit = '{}'::jsonb THEN player_scores.fit ELSE EXCLUDED.fit END, "
        "evaluated_at = COALESCE(EXCLUDED.evaluated_at, player_scores.evaluated_at)").bindparams(
        sa.bindparam("uids", [p[0] for p in pairs], type_=ARRAY(BigInteger)),
        sa.bindparam("sids", [p[1] for p in pairs], type_=ARRAY(Integer)),
        sa.bindparam("comp", comp, type_=ARRAY(sa.Text)), sa.bindparam("fit", fit, type_=ARRAY(sa.Text)),
        sa.bindparam("ev", evaluated_at, type_=sa.DateTime(timezone=True))))


# ---------------------------------------------------------------- the hook

async def on_command(session: AsyncSession, user: User, season_id: int, state: dict[str, Any], *,
                     shift: bool = False) -> list[dict[str, Any]]:
    """Hook of a successful game command, called INSIDE its transaction after the state was written (and, for submitShift,
    after the shift row). Returns the newly unlocked achievements ([] for bots / test users and on any failure)."""
    if not eligible(user):
        return []
    # A plain SAVEPOINT (left open until the transaction ends: releasing it would only cost another round trip) instead of
    # session.begin_nested(), which costs ~2 ms more here: a failure below rolls back to it and the command carries on.
    await session.execute(text("SAVEPOINT ach_hook"))
    try:
        prev = await load_previous(session, [user.id])
        agg: dict[str, Any] = {}
        if shift:
            agg = (await gather_aggregates(session, [user.id], season=False, quiz=False, team=False))[user.id]
        item = Item(user.id, season_id, state, agg)
        results = {user.id: compute(item, prev, cheap=not shift)}
        out = await write_results(session, [item], results, prev, via=VIA_SHIFT if shift else VIA_HOOK)
        return out.get(user.id, [])
    except Exception:
        log.exception("achievements hook failed", extra={"fields": {"user_id": user.id}})
        await session.execute(text("ROLLBACK TO SAVEPOINT ach_hook"))
        return []


# ---------------------------------------------------------------- full evaluation in batches

async def recompute(sm: async_sessionmaker[AsyncSession], *, limit: int = 200, since: datetime | None = None, after: int = 0,
                    force: bool = False, via: str = VIA_RECOMPUTE, dry_run: bool = False) -> dict[str, Any]:
    """Full evaluation (every rule) of up to `limit` real players, ordered by user id, starting after user id `after`.
    Without `force` only players that need it: never fully evaluated, factory written after their last full evaluation,
    or a quiz answer / frozen season result newer than it. `since` additionally keeps only factories written at or after
    that moment. -> {evaluated, failed, granted, points, unlocked: {id: n}, remaining, next_after, ms}."""
    t0 = time.perf_counter()
    started = datetime.now(timezone.utc)  # taken BEFORE reading, so a write during the batch makes the player due again
    sql = (sa.select(Factory.user_id).join(User, User.id == Factory.user_id)
           .outerjoin(PlayerScore, sa.and_(PlayerScore.user_id == Factory.user_id,
                                          PlayerScore.season_id == Factory.season_id))
           .where(~User.is_bot, ~User.is_test, Factory.user_id > after, Factory.state != {}))
    if since is not None:
        sql = sql.where(Factory.updated_at >= since)
    if not force:
        sql = sql.where(sa.or_(
            PlayerScore.evaluated_at.is_(None), Factory.updated_at > PlayerScore.evaluated_at,
            sa.exists().where(QuizAnswer.user_id == Factory.user_id,
                              QuizAnswer.answered_at > PlayerScore.evaluated_at),
            sa.exists().where(SeasonResult.user_id == Factory.user_id,
                              SeasonResult.created_at > PlayerScore.evaluated_at)))
    sql = sql.order_by(Factory.user_id).limit(limit + 1)
    unlocked_count: Counter[str] = Counter()
    summary: dict[str, Any] = {"evaluated": 0, "failed": 0, "granted": 0, "points": 0, "remaining": False, "next_after": after}
    try:
        async with sm() as session, session.begin():
            ids = [r[0] for r in await session.execute(sql)]
            summary["remaining"] = len(ids) > limit
            ids = ids[:limit]
            summary["next_after"] = ids[-1] if ids else after
            if ids:
                prev = await load_previous(session, ids)  # locks the counters first, THEN the states are read
                states = {r.user_id: r for r in await session.execute(
                    sa.select(Factory.user_id, Factory.season_id, Factory.state).where(_any(Factory.user_id, ids)))}
                agg = await gather_aggregates(session, ids)
                items: list[Item] = []
                results: dict[int, dict[str, Any]] = {}
                for uid in ids:
                    row = states.get(uid)
                    if row is None or not isinstance(row.state, dict):
                        continue
                    item = Item(uid, row.season_id, row.state, agg[uid])
                    try:
                        results[uid] = compute(item, prev, cheap=False)
                        items.append(item)
                    except Exception:
                        summary["failed"] += 1
                        log.exception("achievements evaluation failed", extra={"fields": {"user_id": uid}})
                unlocked = await write_results(session, items, results, prev, via=via, evaluated_at=started)
                summary["evaluated"] = len(items)
                for rows in unlocked.values():
                    for a in rows:
                        unlocked_count[a["id"]] += 1
                        summary["granted"] += 1
                        summary["points"] += a["points"]
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass  # everything above was rolled back; the summary says what a real run would have done
    return {**summary, "unlocked": dict(sorted(unlocked_count.items())), "ms": round((time.perf_counter() - t0) * 1000)}
