"""Registry of ALL background jobs.

Spec (Блок. Стек): the bot process with APScheduler is the single owner of periodic work, so that two
competing schedulers never exist. Nothing else in the repo (api, sim) runs timers of its own.

S0: the registry is intentionally empty; the scheduler starts with no jobs. Planned entries (sprints S4-S7):

* season rotation: close a season at ``ends_at``, open the next one an hour later   (S4)
* leaderboard catch-up: lazily advance the top-200 factories every 10-15 min         (S4)
* order reminders: notify consenting players about orders about to finish / expire   (S6)
* HR digest and lead-card outbox delivery                                            (S6)
* 5xx alert: >20 % 5xx over 10 minutes                                               (S6)

To add a job append a ``JobSpec`` to ``JOBS``. Jobs are plain ``async def`` callables; the bot reads the
database directly for analytics, but never touches game state of factories.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

from apscheduler.schedulers.asyncio import AsyncIOScheduler

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class JobSpec:
    id: str
    func: Callable[..., Awaitable[Any]]
    trigger: str  # "interval" | "cron" | "date"
    trigger_args: dict[str, Any] = field(default_factory=dict)  # e.g. {"minutes": 10}
    description: str = ""


JOBS: tuple[JobSpec, ...] = ()


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
            **spec.trigger_args,
        )
        ids.append(spec.id)
    log.info("scheduler jobs registered: %s", ids or "none (S0)")
    return ids
