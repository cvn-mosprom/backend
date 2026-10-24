"""Registry of ALL background jobs.

Spec (Блок. Стек): the bot process with APScheduler is the single owner of periodic work, so that two
competing schedulers never exist. Nothing else in the repo (api, sim) runs timers of its own.

Registered (S4): ``seasons_rotate`` every minute, ``top_advance`` every 12 minutes (+ jitter), ``bots_grow`` every
10 minutes. They only call the api's ``POST /internal/...`` (header ``X-Internal-Token``, base URL ``API_INTERNAL_URL``,
default http://api:8000): the bot never touches factory state itself. Every job logs and swallows its errors (a failing
api, a wrong token or a timeout must never stop the scheduler); ``max_instances=1`` and ``coalesce=True`` so a slow run
never piles up.

C1 adds ``achievements_recompute`` every 10 minutes: ``POST /internal/achievements/recompute`` in batches of 200 (the rules that
need the shifts / quiz / season tables; the per-command rules run inside the api).

C2 adds ``achievement_notify`` every 20 s: the outbox rows ``achievement_unlocked`` become messages to opted-in players (app/player/achievements.py).

Planned entries (sprints S6-S7):

* order reminders (``order_reminders``, 5 min, app/player/reminders.py) and the 5xx alert (``alert_5xx``, 1 min,
  app/player/alerts.py): registered in S6, both idle without BOT_TOKEN
* HR digest                                                                          (S6, open)

To add a job append a ``JobSpec`` to ``JOBS``. Jobs are plain ``async def`` callables; the bot reads the
database directly for analytics, but never touches game state of factories.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from .config import Settings
from .hr.outbox import outbox_delivery
from .player.achievements import achievement_notify
from .player.alerts import alert_5xx
from .player.reminders import order_reminders

log =logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: Callable[..., Awaitable[Any]]
    trigger: str  # "interval" | "cron" | "date"
    trigger_args: dict[str, Any] = field(default_factory=dict)  # e.g. {"minutes": 10}
    description: str = ""


class InternalApiError(Exception):
    """The api answered /internal/* with a non-200 status."""

    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body[:200]}")
        self.status = status


class InternalApi:
    """Minimal client of the api's /internal/* (aiohttp is already a dependency of aiogram)."""

    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    @classmethod
    def from_env(cls, settings: Settings | None = None) -> InternalApi:
        s = settings or Settings.from_env()
        return cls(s.api_internal_url, s.internal_token)

    async def post(self, path: str, payload: dict[str, Any] | None = None, timeout_s: float = 60) -> dict[str, Any]:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as http:
            async with http.post(self.base_url + path, json=payload, headers={"X-Internal-Token": self.token}) as r:
                text = await r.text()
                if r.status != 200:
                    raise InternalApiError(r.status, text)
        data = json.loads(text)
        if not isinstance(data, dict):
            raise InternalApiError(200, f"unexpected body: {text}")
        return data


_api: InternalApi | None = None  # tests inject a client that points at a local server


def set_api(api: InternalApi | None) -> None:
    global _api
    _api = api


def get_api(settings: Settings | None = None) -> InternalApi:
    """The injected client (tests) or the one built from the env / the given settings."""
    return _api or InternalApi.from_env(settings)


async def call_internal(job: str, path: str, payload: dict[str, Any] | None = None,
                        timeout_s: float = 60) -> dict[str, Any] | None:
    """One internal call of a job: returns the JSON answer, or None after logging why it failed. Never raises
    (except task cancellation), so a job can never take the scheduler down."""
    try:
        api = _api or InternalApi.from_env()
        if not api.token:
            log.warning("%s skipped: INTERNAL_TOKEN is not set", job)
            return None
        return await api.post(path, payload, timeout_s)
    except InternalApiError as e:
        log.error("%s failed: %s%s", job, e, " (check INTERNAL_TOKEN)" if e.status == 401 else "")
    except Exception as e:  # api restarting (connection refused), timeout, bad JSON ...: the next tick retries
        log.warning("%s failed: %s: %s", job, type(e).__name__, e)
    return None


async def seasons_rotate() -> dict[str, Any] | None:
    """Close the seasons that ended, open the next one 1 h after the end (idempotent on the api side)."""
    result = await call_internal("seasons_rotate", "/internal/seasons/rotate", timeout_s=120)
    if result and (result.get("closed") or result.get("opened")):
        log.info("seasons_rotate: closed=%s opened=%s", result.get("closed"), result.get("opened"))
    return result


async def top_advance() -> dict[str, Any] | None:
    """Lazy catch-up of the top-200 factories (leaderboard honesty): reason=background, no version bump."""
    result = await call_internal("top_advance", "/internal/top/advance", {"limit": 200}, timeout_s=330)
    if result is not None:
        level = logging.WARNING if result.get("sim_down") or result.get("failed") else logging.INFO
        log.log(level, "top_advance: advanced=%s skipped=%s failed=%s sim_down=%s ms=%s", result.get("advanced"),
                result.get("skipped"), result.get("failed"), result.get("sim_down"), result.get("ms"))
    return result


async def bots_grow() -> dict[str, Any] | None:
    """Grow the demo bots to the real progress of the current season."""
    result = await call_internal("bots_grow", "/internal/bots/grow", timeout_s=60)
    if result is not None:
        log.info("bots_grow: bots=%s season=%s progress=%s", result.get("bots"), result.get("season_id"),
                 result.get("progress"))
    return result


ACHIEVEMENT_BATCH = 200  # players per call
ACHIEVEMENT_MAX_CALLS = 20  # per run: 4000 players; the rest follows on the next run (the api resumes by "due" players)


async def achievements_recompute() -> dict[str, Any] | None:
    """Full achievement evaluation (shift, quiz and season rules the per-command hook does not run) of the players that changed
    since their last one. Calls the api in batches of 200 following ``remaining`` / ``next_after``; None when the first call failed."""
    total: dict[str, Any] = {"evaluated": 0, "granted": 0, "failed": 0, "calls": 0}
    after = 0
    for _ in range(ACHIEVEMENT_MAX_CALLS):
        r = await call_internal("achievements_recompute", "/internal/achievements/recompute",
                                {"limit": ACHIEVEMENT_BATCH, "after": after}, timeout_s=120)
        if r is None:
            return total if total["calls"] else None
        total["calls"] += 1
        for k in ("evaluated", "granted", "failed"):
            total[k] += int(r.get(k) or 0)
        after = int(r.get("next_after") or after)
        if not r.get("remaining"):
            break
    if total["evaluated"] or total["failed"]:
        log.log(logging.WARNING if total["failed"] else logging.INFO, "achievements_recompute: evaluated=%s granted=%s failed=%s calls=%s",
                total["evaluated"], total["granted"], total["failed"], total["calls"])
    return total


JOBS: tuple[JobSpec, ...] = (
    JobSpec("seasons_rotate", seasons_rotate, "interval", {"minutes": 1}, "season rotation (close / open)"),
    JobSpec("top_advance", top_advance, "interval", {"minutes": 12, "jitter": 90}, "top-200 background catch-up"),
    JobSpec("bots_grow", bots_grow, "interval", {"minutes": 10, "jitter": 30}, "demo bots growth"),
    # S6: lead cards / revocation notices to HR (does nothing without BOT_TOKEN, HR_ADMIN_TG_IDS or DATABASE_URL)
    JobSpec("outbox_delivery", outbox_delivery, "interval", {"seconds": 7}, "HR outbox delivery (lead cards)"),
    # S6 player side (both do nothing without BOT_TOKEN): order reminders for opted-in players, 5xx alert for the HR admins
    JobSpec("order_reminders", order_reminders, "interval", {"minutes": 5, "jitter": 20}, "order reminders (opted-in players)"),
    JobSpec("alert_5xx", alert_5xx, "interval", {"minutes": 1}, "5xx alert (>20% of 10 min) to HR admins"),
    # C2: gold / seasonal achievement notifications to opted-in players (outbox kind achievement_unlocked)
    JobSpec("achievement_notify", achievement_notify, "interval", {"seconds": 20}, "achievement notifications (gold / seasonal)"),
    # C1: full achievement evaluation (shift / quiz / season rules) for the players that changed
    JobSpec("achievements_recompute", achievements_recompute, "interval", {"minutes": 10, "jitter": 30},
            "achievements full recompute (batches)"),
)


def build_scheduler() -> AsyncIOScheduler:
    # UTC everywhere; coalesce + max_instances=1 so a slow run never piles up.
    return AsyncIOScheduler(
        timezone="UTC",
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": 60},
    )


def register_jobs(scheduler: AsyncIOScheduler, jobs: Iterable[JobSpec] | None = None) -> list[str]:
    """Add every job of the registry to the scheduler; returns the registered ids."""
    ids: list[str] = []
    for spec in JOBS if jobs is None else jobs:
        scheduler.add_job(
            spec.func,
            trigger=spec.trigger,
            id=spec.id,
            name=spec.description or spec.id,
            replace_existing=True,
            max_instances=1,
            coalesce=True,
            **spec.trigger_args,
        )
        ids.append(spec.id)
    log.info("scheduler jobs registered: %s", ids or "none")
    return ids
