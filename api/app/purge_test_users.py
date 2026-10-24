"""Delete every test user (users.is_test) and all their data: `python -m app.purge_test_users [--dry-run]`.

Run it before the demo (inside compose: `docker compose exec api python -m app.purge_test_users`). Foreign keys cascade
the factories, shifts, leads, quiz answers, season results and reminders; analytics events (ON DELETE SET NULL) and the
pending outbox rows of those users are deleted explicitly; the Redis leaderboard caches are dropped (best effort).
Demo bots (is_bot) and real players are never touched. Prints a one-line JSON summary.
"""
from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from .db import make_engine, make_sessionmaker
from .settings import get_settings

COUNTS = {
    "users": "SELECT count(*) FROM users WHERE is_test",
    "factories": "SELECT count(*) FROM factories f JOIN users u ON u.id = f.user_id WHERE u.is_test",
    "leads": "SELECT count(*) FROM leads l JOIN users u ON u.id = l.user_id WHERE u.is_test",
    "events": "SELECT count(*) FROM events e JOIN users u ON u.id = e.user_id WHERE u.is_test",
    "outbox": "SELECT count(*) FROM outbox o WHERE (o.payload->>'user_id') IN (SELECT id::text FROM users WHERE is_test)",
}


async def purge(session: AsyncSession, *, dry_run: bool = False) -> dict[str, int]:
    """Runs in the caller's transaction. Returns what was (or, with dry_run, would be) deleted."""
    out = {k: int((await session.execute(text(q))).scalar_one()) for k, q in COUNTS.items()}
    if not dry_run and out["users"]:
        await session.execute(text("DELETE FROM events WHERE user_id IN (SELECT id FROM users WHERE is_test)"))
        await session.execute(text(COUNTS["outbox"].replace("SELECT count(*)", "DELETE", 1)))
        await session.execute(text("DELETE FROM users WHERE is_test"))
    return out


async def drop_caches(redis_url: str) -> int:
    """Leaderboard caches (`lb:*`); a failure is not an error (they expire within seconds anyway)."""
    try:
        from redis.asyncio import Redis

        r = Redis.from_url(redis_url)
        try:
            keys = [k async for k in r.scan_iter("lb:*")]
            return int(await r.delete(*keys)) if keys else 0
        finally:
            await r.aclose()
    except Exception:
        return 0


async def main(argv: list[str] | None = None) -> dict[str, Any]:
    ap = argparse.ArgumentParser(prog="python -m app.purge_test_users", description="Delete e2e / test users and their data")
    ap.add_argument("--dry-run", action="store_true", help="only count what would be deleted")
    args = ap.parse_args(argv)
    settings = get_settings()
    engine = make_engine(settings, timeouts=False)  # bulk deletes: no 3 s statement timeout
    try:
        async with make_sessionmaker(engine)() as session, session.begin():
            result: dict[str, Any] = await purge(session, dry_run=args.dry_run)
    finally:
        await engine.dispose()
    result["dry_run"] = args.dry_run
    if not args.dry_run and settings.redis_url:
        result["cache_keys_dropped"] = await drop_caches(settings.redis_url)
    print(json.dumps(result))
    return result


if __name__ == "__main__":
    asyncio.run(main())
