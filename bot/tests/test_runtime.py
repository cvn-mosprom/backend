"""Process-level behaviour: scheduler with no jobs, idle without a token (no crash-loop)."""

from __future__ import annotations

import asyncio

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import Settings
from app.jobs import JOBS, JobSpec, build_scheduler, register_jobs
from app.main import amain, make_bot


def test_job_registry_has_the_s4_jobs():
    ids = [j.id for j in JOBS]
    assert ids[:3] == ["seasons_rotate", "top_advance", "bots_grow"] and "outbox_delivery" in ids


def test_register_jobs_registry_and_custom():
    async def run() -> tuple[list[str], list[str]]:
        scheduler = build_scheduler()
        assert isinstance(scheduler, AsyncIOScheduler)
        assert register_jobs(scheduler, []) == []
        assert scheduler.get_jobs() == []

        async def noop() -> None: ...

        ids = register_jobs(scheduler, [JobSpec("t", noop, "interval", {"minutes": 5}, "test job")])
        return ids, [j.id for j in scheduler.get_jobs()]

    ids, scheduled = asyncio.run(run())
    assert ids == scheduled == ["t"]


def test_make_bot_without_or_with_malformed_token_returns_none():
    assert make_bot(Settings(bot_token="")) is None
    assert make_bot(Settings(bot_token="not-a-token")) is None


def test_make_bot_with_wellformed_token():
    bot = make_bot(Settings(bot_token="123456:ABCdefGhIJKlmNoPQRsTUVwxyZ-0123456"))
    assert bot is not None and bot.id == 123456


def test_amain_idles_without_token_until_stopped():
    async def run() -> int:
        stop = asyncio.Event()
        task = asyncio.create_task(amain(Settings(bot_token=""), stop))
        await asyncio.sleep(0.2)
        assert not task.done(), "bot must idle (not exit) without BOT_TOKEN"
        stop.set()
        return await asyncio.wait_for(task, timeout=5)

    assert asyncio.run(run()) == 0
