"""Who may use the HR bot (C3): the api's ``hr_users`` table, read directly (the bot never calls the api for this).

Roles, lowest first: ``analyst`` < ``hr`` < ``hr_lead`` < ``admin`` (see api/app/hr/access.py). In the bot:

* any active role may use the aggregate commands (/hr /funnel /sources /stats /top: no personal data);
* ``hr``+ sees candidates (/leads /lead, pages) and uses the status buttons of a card; ``analyst`` does not (PII);
* ``hr_lead``+ may /export; lead cards (outbox) go to HR_CHAT_ID, else to every active ``hr``/``hr_lead``/``admin``;
* the 5xx alert goes to the active ``admin`` rows.

``HR_ADMIN_TG_IDS`` is only the bootstrap: it is consulted when ``hr_users`` has no rows at all or cannot be read
(api not migrated / seeded yet, database hiccup), and then every listed id counts as ``admin``.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
from typing import Any

from sqlalchemy import text

log = logging.getLogger(__name__)

ROLE_RANK: dict[str, int] = {"analyst": 0, "hr": 1, "hr_lead": 2, "admin": 3}
CARD_ROLES = ("hr", "hr_lead", "admin")  # roles that receive lead cards (they carry personal data)


def at_least(role: str | None, min_role: str) -> bool:
    return role is not None and ROLE_RANK.get(role, -1) >= ROLE_RANK[min_role]


async def role_of(sm: Any, env_admins: Collection[int], tg_id: int | None) -> str | None:
    """The active role of ``tg_id`` or None (unknown, deactivated, no sender). Env fallback: see the module docstring."""
    if tg_id is None:
        return None
    fallback = "admin" if tg_id in env_admins else None
    if sm is None:
        return fallback
    try:
        async with sm() as s:
            row = (await s.execute(
                text("SELECT (SELECT role FROM hr_users WHERE tg_id = :tg AND active) AS role, "
                     "EXISTS (SELECT 1 FROM hr_users) AS has_rows"), {"tg": tg_id})).first()
    except Exception as e:
        log.warning("hr_users unreadable (%s): falling back to HR_ADMIN_TG_IDS", type(e).__name__)
        return fallback
    if row is None or not row.has_rows:
        return fallback
    return row.role


async def _rows(sm: Any) -> list[Any]:
    if sm is None:
        return []
    try:
        async with sm() as s:
            return list((await s.execute(text("SELECT tg_id, role, active FROM hr_users ORDER BY tg_id"))).all())
    except Exception as e:
        log.warning("hr_users unreadable (%s): falling back to HR_ADMIN_TG_IDS", type(e).__name__)
        return []


async def card_recipients(sm: Any, env_admins: Collection[int]) -> list[int]:
    """Private chats for lead cards when HR_CHAT_ID is not set."""
    rows = await _rows(sm)
    if rows:
        return [r.tg_id for r in rows if r.active and r.role in CARD_ROLES]
    return sorted(env_admins)


async def admin_ids(sm: Any, env_admins: Collection[int]) -> frozenset[int]:
    """Active admins (recipients of the 5xx alert)."""
    rows = await _rows(sm)
    if rows:
        return frozenset(r.tg_id for r in rows if r.active and r.role == "admin")
    return frozenset(env_admins)


async def actor_hr_id(s: Any, tg_id: int) -> int | None:
    """hr_users.id for lead_events.actor_hr_id (None when the person has no row, e.g. env fallback)."""
    return (await s.execute(text("SELECT id FROM hr_users WHERE tg_id = :tg"), {"tg": tg_id})).scalar_one_or_none()
