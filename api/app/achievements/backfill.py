"""One-off backfill: full evaluation of every real player (the ones that existed before achievements): `python -m app.achievements.backfill
[--batch 200] [--dry-run]` (compose: `docker compose exec api python -m app.achievements.backfill`).

Idempotent and resumable: it runs the same `service.recompute` the bot job uses, with `force=True` (players that are up to date are
evaluated again, which grants nothing new), granted_via = backfill. Prints one JSON line:
{users, granted, points, failed, unlocked: {achievement id: count}, batches, dry_run, ms}. Bots and test users are skipped.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import time
from collections import Counter
from typing import Any

from ..db import make_engine, make_sessionmaker
from ..settings import get_settings
from . import service


async def run(sm: Any, *, batch: int = 200, dry_run: bool = False) -> dict[str, Any]:
    t0 = time.perf_counter()
    totals: Counter[str] = Counter()
    after, batches = 0, 0
    while True:
        r = await service.recompute(sm, limit=batch, after=after, force=True, via=service.VIA_BACKFILL, dry_run=dry_run)
        batches += 1
        totals.update(r["unlocked"])
        for k in ("evaluated", "failed", "granted", "points"):
            totals["_" + k] += r[k]
        after = r["next_after"]
        if not r["remaining"]:
            break
    users, failed, granted, points = (totals.pop("_" + k, 0) for k in ("evaluated", "failed", "granted", "points"))
    return {"users": users, "granted": granted, "points": points, "failed": failed, "unlocked": dict(sorted(totals.items())),
            "batches": batches, "dry_run": dry_run, "ms": round((time.perf_counter() - t0) * 1000)}


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(prog="python -m app.achievements.backfill", description="Evaluate achievements for every player")
    ap.add_argument("--batch", type=int, default=200, help="players per transaction (1..1000)")
    ap.add_argument("--dry-run", action="store_true", help="evaluate and report, write nothing")
    args = ap.parse_args(argv)
    engine = make_engine(get_settings(), timeouts=False)  # a long batch: no 3 s statement timeout
    try:
        result = await run(make_sessionmaker(engine), batch=max(1, min(1000, args.batch)), dry_run=args.dry_run)
    finally:
        await engine.dispose()
    print(json.dumps(result, ensure_ascii=False))
    return result


if __name__ == "__main__":
    asyncio.run(main())
