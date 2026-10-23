"""Demo bots for the leaderboard (spec block 5): `python -m app.seed_bots [--count 40] [--progress P] [--floor F]`.

Idempotent: bot i is always the same user (tg_id = BOT_TG_BASE - i), so a rerun only creates the missing ones and
re-grows all of them. Bots are users with `is_bot=true` and a factory whose `state` is a placeholder (they never go
through the sim):
    {"bot": {"v": 1, "final": <stars at season end>, "g": <curve exponent>, "quality": 0..1, "speed": <bottles/h>,
             "lines": 1..3, "lemonade": bool},
     "sim": {"layout": <generated layout, shown by GET /api/factory/{id}>}}
Growth curve (port of vendor/frontend/web/src/lib/api/mock/bots.ts): stars = final * p^g with p = season progress 0..1;
`grow_bots()` writes stars / quality_avg / speed_bph (+ level, credits, xp) for the given progress and never lowers a
value inside the same season - the later background job just calls it with the real progress.
Layouts come from data/bot_layouts.json, generated once with the mock's own botLayout() (LayoutBuilder / compactLine).
BOTS_ENABLED=0 hides every bot from the leaderboard, ranks and visits (the rows stay in the database).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from .db import make_engine, make_sessionmaker
from .game import GameService
from .models import Factory, Season, User
from .settings import get_settings
from .sim_client import now_ms

BOT_TG_BASE = -1_000_000  # real Telegram ids are positive: bots can never collide with a player
DEFAULT_COUNT = 40
LAYOUTS_FILE = Path(__file__).parent / "data" / "bot_layouts.json"

NICK_A = ["kvas", "linia", "tarhun", "sirop", "bottle", "probka", "butyl", "zavod", "smena", "master", "ceh", "kipia",
          "otk", "rampa", "gruz", "duchess", "brodilo", "pena", "gaz", "etiketka"]
NICK_B = ["master", "queen", "king", "pro", "hunter", "boss", "life", "fan", "_2006", "_x", "man", "girl", "ne_tormozi",
          "top", "lead", "baron", "kid", "ok", "3000", "777"]
CEH = ["Цех", "Линия", "Завод", "Участок", "Цех №"]
CEH_N = ["Вани", "Маши", "Кирилла", "Олега", "Насти", "Тимура", "Лены", "Артёма", "Дины", "Саши", "Ромы", "Веры", "Гоши",
         "Юли", "Миши"]
UNIVERSITIES = [  # content/universities.ts
    "РОСБИОТЕХ", "РТУ МИРЭА", "МГТУ им. Баумана", "МГУ им. Ломоносова", "НИУ ВШЭ", "РХТУ им. Менделеева", "МИСИС",
    "МЭИ", "Московский Политех", "РЭУ им. Плеханова", "МАИ", "РУДН", "Финансовый университет", "МИФИ", "МФТИ",
    "Губкинский университет", "Станкин", "РАНХиГС", "Колледж (СПО)", "Другой вуз"]


def gen_bot(i: int, seed: str = "mosprom-bots") -> dict[str, Any]:
    """Deterministic bot #i (independent of how many bots are generated): identity + growth curve."""
    r = random.Random(f"{seed}:{i}")
    u, v = max(1e-6, r.random()), r.random()
    z = math.sqrt(-2 * math.log(u)) * math.cos(2 * math.pi * v)
    final = round(math.exp(7.3 + 0.85 * z))  # log-normal: median ~1500 stars, tail to ~15000
    lines = 3 if final > 6000 else 2 if final > 2500 else 1
    nick = r.choice(NICK_A) + r.choice(NICK_B) + (str(int(r.random() * 99)) if r.random() < 0.3 else "")
    ceh = r.choice(CEH)
    factory = f"Цех №{1 + int(r.random() * 40)}" if ceh == "Цех №" else f"{ceh} {r.choice(CEH_N)}"
    return {
        "nick": nick, "factory": factory,
        "university": UNIVERSITIES[int(r.random() ** 1.6 * (len(UNIVERSITIES) - 1))],
        "curve": {"v": 1, "final": final, "g": round(0.8 + r.random() * 0.8, 3),
                  "quality": round(min(0.995, 0.86 + r.random() * 0.1 + (0.03 if final > 4000 else 0)), 3),
                  "speed": round(1400 * lines * (0.8 + r.random() * 1.6)), "lines": lines,
                  "lemonade": r.random() < 0.4 + lines * 0.15},
    }


def bot_values(curve: dict[str, Any], progress: float) -> dict[str, int | float]:
    """Denormalised columns of a bot at season progress 0..1 (same shape of curves as the mock's board())."""
    p = max(0.0, min(1.0, progress))
    stars = round(curve["final"] * p ** curve["g"])
    return {
        "stars": stars,
        "quality_avg": round(curve["quality"] * min(1.0, 0.6 + p), 4),
        "speed_bph": round(curve["speed"] * min(1.0, 0.15 + p * 1.2)),
        "level": min(10, 1 + curve["lines"] * 3),
        "credits": 3000 + stars * 2,
        "xp": stars // 2,
        "bottles_season": stars * 20,
    }


def pick_layout(layouts: list[dict[str, Any]], curve: dict[str, Any], i: int) -> dict[str, Any]:
    same = [x for x in layouts if x["lines"] == curve["lines"] and x["lemonade"] == curve["lemonade"]]
    same = same or [x for x in layouts if x["lines"] == curve["lines"]] or layouts
    return same[i % len(same)]["layout"]


def season_progress(season: Season, wall_ms: int) -> float:
    start, end = season.starts_at.timestamp() * 1000, season.ends_at.timestamp() * 1000
    return max(0.0, min(1.0, (wall_ms - start) / (end - start))) if end > start else 1.0


async def grow_bots(session: AsyncSession, season_id: int, progress: float) -> int:
    """Set the columns of every bot factory for `progress`; inside one season a value never goes down (the first time
    a bot lands in a season it starts from the curve). Returns the number of bots touched."""
    rows = (await session.execute(
        select(Factory.user_id, Factory.season_id, Factory.state, Factory.stars, Factory.quality_avg, Factory.speed_bph)
        .where(Factory.is_bot.is_(True)))).all()
    touched = 0
    for r in rows:
        curve = (r.state or {}).get("bot")
        if not isinstance(curve, dict):
            continue
        v = bot_values(curve, progress)
        if r.season_id == season_id:  # monotonic within the season
            v["stars"] = max(v["stars"], r.stars)
            v["speed_bph"] = max(v["speed_bph"], r.speed_bph)
            v["quality_avg"] = max(v["quality_avg"], r.quality_avg)
        await session.execute(update(Factory).where(Factory.user_id == r.user_id)
                              .values(season_id=season_id, updated_at=datetime.now(timezone.utc), **v))
        touched += 1
    return touched


async def seed(session: AsyncSession, count: int = DEFAULT_COUNT, progress: float | None = None,
               floor: float = 0.2) -> dict[str, Any]:
    """Create the missing bots (ids 0..count-1) in the open season and grow all bots. One transaction (the caller's)."""
    layouts = json.loads(LAYOUTS_FILE.read_text(encoding="utf-8"))
    wall = now_ms()
    season_id = await GameService._ensure_open_season(session, wall)
    season = (await session.execute(select(Season).where(Season.id == season_id))).scalar_one()
    p = max(season_progress(season, wall), floor) if progress is None else progress
    existing = set((await session.execute(select(User.tg_id).where(User.is_bot.is_(True)))).scalars())
    created = 0
    for i in range(count):
        tg_id = BOT_TG_BASE - i
        if tg_id in existing:
            continue
        b = gen_bot(i)
        uid = (await session.execute(
            pg_insert(User).values(tg_id=tg_id, username=f"bot_{i}", first_name=b["nick"], university=b["university"],
                                   age_confirmed=True, is_bot=True, source="bot")
            .on_conflict_do_nothing(index_elements=[User.tg_id]).returning(User.id))).scalar_one_or_none()
        if uid is None:  # lost a race with another seeding run
            continue
        state = {"bot": b["curve"], "sim": {"layout": pick_layout(layouts, b["curve"], i)}}
        await session.execute(pg_insert(Factory).values(
            user_id=uid, season_id=season_id, name=b["factory"], state=state, university=b["university"], is_bot=True,
            **bot_values(b["curve"], 0)).on_conflict_do_nothing(index_elements=[Factory.user_id]))
        created += 1
    touched = await grow_bots(session, season_id, p)
    return {"created": created, "bots": touched, "season_id": season_id, "progress": round(p, 3)}


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(prog="python -m app.seed_bots", description="Seed / grow the demo leaderboard bots")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT, help="number of bots (default 40; spec: 30-50)")
    ap.add_argument("--progress", type=float, help="season progress 0..1 to grow the bots to (default: real, see --floor)")
    ap.add_argument("--floor", type=float, default=0.2, help="minimum progress when --progress is not given (demo: a "
                                                              "fresh season must not show an all-zero board); 0 = off")
    args = ap.parse_args(argv)
    if not 1 <= args.count <= 500:
        ap.error("--count must be 1..500")
    engine = make_engine(get_settings())
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            result = await seed(session, args.count, args.progress, args.floor)
    finally:
        await engine.dispose()
    print(f"bots: created {result['created']}, total {result['bots']}, season {result['season_id']}, "
          f"progress {result['progress']}")
    return result


if __name__ == "__main__":
    asyncio.run(main())
