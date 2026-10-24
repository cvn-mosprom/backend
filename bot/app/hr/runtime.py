"""Process-wide HR state (settings, Telegram bot, DB session factory) shared by the handlers and the outbox job.

``main.amain`` calls ``setup`` once; scheduler jobs are plain argument-less callables, so they read it from here.
Tests call ``setup`` with a mocked bot and their own sessionmaker.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from ..config import Settings
from .db import make_sessionmaker

log = logging.getLogger(__name__)


@dataclass
class Runtime:
    settings: Settings
    bot: Any | None  # aiogram Bot; None without a usable token
    sm: async_sessionmaker[AsyncSession] | None  # None without DATABASE_URL

    @property
    def admins(self) -> frozenset[int]:
        return self.settings.hr_admin_tg_ids

    def is_admin(self, tg_id: int | None) -> bool:
        return tg_id is not None and tg_id in self.settings.hr_admin_tg_ids

    def recipients(self) -> list[int]:
        """Chats that get lead cards: HR_CHAT_ID when set, else every admin."""
        if self.settings.hr_chat_id is not None:
            return [self.settings.hr_chat_id]
        return sorted(self.settings.hr_admin_tg_ids)


_rt: Runtime | None = None


def setup(settings: Settings, bot: Any | None, sm: async_sessionmaker[AsyncSession] | None = None) -> Runtime:
    global _rt
    if sm is None:
        sm = make_sessionmaker(settings)
    if sm is None:
        log.warning("DATABASE_URL is empty: the HR features (outbox delivery, /leads, ...) are disabled")
    if not settings.hr_admin_tg_ids:
        log.warning("HR_ADMIN_TG_IDS is empty: nobody can use the HR commands and lead cards have no recipient")
    _rt = Runtime(settings=settings, bot=bot, sm=sm)
    return _rt


def get() -> Runtime | None:
    return _rt


async def close() -> None:
    global _rt
    rt, _rt = _rt, None
    bind = rt.sm.kw.get("bind") if rt is not None and rt.sm is not None else None
    if bind is not None:
        await bind.dispose()
