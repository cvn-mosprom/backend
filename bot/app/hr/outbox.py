"""Outbox delivery: the api inserts ``lead_created`` / ``lead_revoked`` rows in the transaction of the business change,
this job sends them to HR and marks ``sent_at``.

* rows are claimed one at a time with ``FOR UPDATE SKIP LOCKED`` (the lock lives only while that row is sent), so two
  workers never send the same card and a slow Telegram never blocks the rest of the table;
* failure -> ``attempts += 1`` + ``last_error``, retry with exponential backoff (kept in memory: the table has no
  next-attempt column and migrations are out of scope), at most ``MAX_ATTEMPTS`` tries;
* a row counts as delivered when at least one recipient got it (a recipient that blocked the bot must not make the row
  retry forever); failures of the others are written to ``last_error``;
* without a bot (no BOT_TOKEN), without recipients or without a database the job does NOT touch the outbox: the rows
  wait until the bot is configured;
* C4: ``hr_message`` rows (HR writes to a candidate from the CRM) go to the candidate's own chat, not to the HR chats:
  ``deliver_messages``. They need no HR recipients, only a bot and a database;
* the job never raises (Telegram down, DB down ...): the scheduler must keep running.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any

from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError
from sqlalchemy import text

from . import queries, runtime
from .format import PROFESSIONS, card_from_payload, card_keyboard, render_card, render_revoked_notice

log = logging.getLogger(__name__)

KINDS = ("lead_created", "lead_revoked")
MAX_ATTEMPTS = 50
BATCH = 10
SEND_TIMEOUT_S = 20
BACKOFF_BASE_S = 10
BACKOFF_MAX_S = 300
ERROR_MAX = 500

_next_try: dict[int, float] = {}  # outbox id -> time.monotonic() before which it is not retried
_warned: set[str] = set()


@dataclass
class DeliveryResult:
    sent: int = 0
    failed: int = 0
    test_skipped: int = 0  # rows of test users (users.is_test): marked sent, never shown to HR
    skipped: str | None = None  # why nothing was attempted
    errors: list[str] = field(default_factory=list)


def backoff_s(attempts: int, retry_after: float | None = None) -> float:
    delay = min(BACKOFF_MAX_S, BACKOFF_BASE_S * 2 ** max(attempts - 1, 0))
    return max(delay, retry_after or 0)


def _warn_once(key: str, msg: str) -> None:
    if key not in _warned:
        _warned.add(key)
        log.warning(msg)


def _err(chat_id: int, e: BaseException) -> str:
    return f"{chat_id}: {type(e).__name__}: {str(e)[:150]}"


async def _send_all(rt: runtime.Runtime, text_: str, markup: Any, chats: list[int]) -> tuple[int, list[str], float | None]:
    ok, errors, retry_after = 0, [], None
    for chat in chats:
        try:
            await asyncio.wait_for(rt.bot.send_message(chat, text_, reply_markup=markup), SEND_TIMEOUT_S)
            ok += 1
        except asyncio.CancelledError:
            raise
        except Exception as e:  # Telegram unavailable, flood limit, blocked bot, timeout ...
            errors.append(_err(chat, e))
            ra = getattr(e, "retry_after", None)
            if isinstance(ra, (int, float)):
                retry_after = max(retry_after or 0, float(ra))
    return ok, errors, retry_after


async def _is_test_user(session: Any, payload: Any) -> bool:
    """e2e / test players (users.is_test, S13) produce lead rows too; HR must not get their cards."""
    uid = payload.get("user_id") if isinstance(payload, dict) else None
    if not isinstance(uid, int) or isinstance(uid, bool):
        return False
    return bool((await session.execute(text("SELECT is_test FROM users WHERE id = :id"), {"id": uid})).scalar_one_or_none())


async def _render(session: Any, kind: str, payload: dict[str, Any]) -> tuple[str, Any]:
    """Message text + keyboard for one row, from the lead's current state (falls back to the payload)."""
    lead_id = payload.get("lead_id")
    card = None
    if isinstance(lead_id, int):
        card = await queries.fetch_lead(session, lead_id)
    if card is None:
        card = card_from_payload(payload)
    if kind == "lead_revoked":
        return render_revoked_notice(card), None
    return render_card(card, new=True), card_keyboard(card)


async def deliver_once(rt: runtime.Runtime, limit: int = BATCH) -> DeliveryResult:
    res = DeliveryResult()
    if rt.bot is None:
        res.skipped = "no_bot"
        return res
    if rt.sm is None:
        res.skipped = "no_db"
        return res
    chats = await rt.recipients()
    if not chats:
        res.skipped = "no_recipients"
        _warn_once("recipients", "outbox delivery paused: HR_ADMIN_TG_IDS / HR_CHAT_ID name nobody to send lead cards to")
        return res
    for _ in range(limit):
        now = time.monotonic()
        skip = [i for i, t in _next_try.items() if t > now]
        async with rt.sm() as session, session.begin():
            row = (await session.execute(text(
                "SELECT id, kind, payload, attempts FROM outbox WHERE sent_at IS NULL AND attempts < :max "
                "AND kind = ANY(CAST(:kinds AS text[])) AND NOT (id = ANY(CAST(:skip AS bigint[]))) "
                "ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED"),
                {"max": MAX_ATTEMPTS, "kinds": list(KINDS), "skip": skip})).first()
            if row is None:
                break
            if await _is_test_user(session, row.payload):
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = 'test user: not delivered' "
                                           "WHERE id = :id"), {"id": row.id})
                res.test_skipped += 1
                continue
            text_, markup = await _render(session, row.kind, row.payload or {})
            ok, errors, retry_after = await _send_all(rt, text_, markup, chats)
            err = "; ".join(errors)[:ERROR_MAX] or None
            if ok:
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = :err WHERE id = :id"),
                                      {"err": err, "id": row.id})
                _next_try.pop(row.id, None)
                res.sent += 1
                continue
            attempts = row.attempts + 1
            await session.execute(text("UPDATE outbox SET attempts = attempts + 1, last_error = :err WHERE id = :id"),
                                  {"err": err, "id": row.id})
            _next_try[row.id] = time.monotonic() + backoff_s(attempts, retry_after)
            res.failed += 1
            res.errors.extend(errors)
            if attempts >= MAX_ATTEMPTS:
                log.error("outbox row %s gave up after %s attempts: %s", row.id, attempts, err)
            # Telegram itself is probably unreachable: do not burn the attempts of the other rows in this tick
            break
    return res


# ---------------------------------------------------------------- C4: HR -> candidate messages

MESSAGE_KIND = "hr_message"
MESSAGE_MAX_ATTEMPTS = 20


def default_message(template: str, first_name: str | None, vacancy: str | None) -> str | None:
    """Default Russian text of a CRM template (used when the HR sent the template without editing it). None for `custom`."""
    name = (first_name or "").strip()
    role = PROFESSIONS.get(vacancy or "")
    hello = f"Здравствуйте, {name}!" if name else "Здравствуйте!"
    where = f" на позицию «{role}»" if role else ""
    if template == "interview":
        return f"{hello} Приглашаем вас на интервью в «Очаково»{where}. Напишите, когда вам удобно, и мы согласуем время."
    if template == "offer":
        return f"{hello} Рады сообщить: мы готовы предложить вам место в «Очаково»{where}. Детали пришлём сюда же."
    if template == "reject":
        return (f"{hello} Спасибо за интерес к «Очаково». Сейчас мы не готовы предложить вам место, "
                "но сохраним вашу анкету в резерве.")
    return None


def message_text(payload: dict[str, Any]) -> str | None:
    """What the candidate receives: the text HR typed, else the template default; None = nothing to send."""
    text_ = payload.get("text")
    if isinstance(text_, str) and text_.strip():
        return text_.strip()
    return default_message(str(payload.get("template")), payload.get("first_name"), payload.get("vacancy"))


def message_skip_reason(payload: Any, row: Any) -> str | None:
    """Why an `hr_message` must not be sent (None = send it). `row` carries uid, tg_id, is_bot, is_test, bot_started_at, lead_revoked."""
    if not isinstance(payload, dict) or message_text(payload) is None:
        return "malformed payload"
    if row.uid is None:
        return "no such user"
    if row.is_bot or row.is_test:
        return "bot or test user"
    if row.lead_revoked:
        return "consent revoked"
    if row.bot_started_at is None:
        return "bot not started"
    if not isinstance(row.tg_id, int) or row.tg_id <= 0:
        return "no chat"
    return None


_CLAIM_MESSAGE = text(
    "SELECT o.id, o.payload, o.attempts, u.id AS uid, u.tg_id, u.is_bot, u.is_test, u.bot_started_at, "
    "(l.consent_revoked_at IS NOT NULL) AS lead_revoked FROM outbox o "
    "LEFT JOIN users u ON u.id = CASE WHEN jsonb_typeof(o.payload -> 'user_id') = 'number' THEN (o.payload ->> 'user_id')::bigint END "
    "LEFT JOIN leads l ON l.id = CASE WHEN jsonb_typeof(o.payload -> 'lead_id') = 'number' THEN (o.payload ->> 'lead_id')::bigint END "
    "WHERE o.kind = :kind AND o.sent_at IS NULL AND o.attempts < :max AND NOT (o.id = ANY(CAST(:skip AS bigint[]))) "
    "ORDER BY o.id LIMIT 1 FOR UPDATE OF o SKIP LOCKED")


async def deliver_messages(rt: runtime.Runtime, limit: int = BATCH) -> DeliveryResult:
    """Send pending ``hr_message`` rows to the candidates' chats (plain text: HR text may contain markup characters).
    Every row ends with ``sent_at`` set - delivered, or skipped with the reason in ``last_error`` (revoked consent, blocked bot ...);
    a failed send is retried with the in-memory backoff, at most ``MESSAGE_MAX_ATTEMPTS`` times. ``test_skipped`` counts the skipped."""
    res = DeliveryResult()
    if rt.bot is None:
        res.skipped = "no_bot"
        return res
    if rt.sm is None:
        res.skipped = "no_db"
        return res
    for _ in range(limit):
        skip = [i for i, t in _next_try.items() if t > time.monotonic()]
        async with rt.sm() as session, session.begin():
            row = (await session.execute(_CLAIM_MESSAGE, {"kind": MESSAGE_KIND, "max": MESSAGE_MAX_ATTEMPTS, "skip": skip})).first()
            if row is None:
                break
            payload = row.payload if isinstance(row.payload, dict) else {}
            reason = message_skip_reason(row.payload, row)
            if reason is not None:
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = :err WHERE id = :id"),
                                      {"err": f"not delivered: {reason}", "id": row.id})
                res.test_skipped += 1
                continue
            try:
                await asyncio.wait_for(rt.bot.send_message(row.tg_id, message_text(payload), parse_mode=None), SEND_TIMEOUT_S)
            except TelegramForbiddenError:  # the candidate blocked the bot: stop asking
                await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = 'not delivered: blocked the bot' WHERE id = :id"),
                                      {"id": row.id})
                res.test_skipped += 1
                continue
            except asyncio.CancelledError:
                raise
            except (TelegramAPIError, OSError, asyncio.TimeoutError) as e:
                err = f"{type(e).__name__}: {str(e)[:150]}"
                await session.execute(text("UPDATE outbox SET attempts = attempts + 1, last_error = :err WHERE id = :id"),
                                      {"err": err, "id": row.id})
                ra = getattr(e, "retry_after", None)
                _next_try[row.id] = time.monotonic() + backoff_s(row.attempts + 1, float(ra) if isinstance(ra, (int, float)) else None)
                res.failed += 1
                res.errors.append(err)
                break  # Telegram is probably unreachable: do not burn the attempts of the other rows
            await session.execute(text("UPDATE outbox SET sent_at = now(), last_error = NULL WHERE id = :id"), {"id": row.id})
            _next_try.pop(row.id, None)
            res.sent += 1
    return res


async def outbox_delivery() -> DeliveryResult | None:
    """Scheduler job (every few seconds). Never raises."""
    rt = runtime.get()
    if rt is None:
        return None
    try:
        res = await deliver_once(rt)
        msgs = await deliver_messages(rt)  # C4: independent of the HR recipients
        res.sent += msgs.sent
        res.failed += msgs.failed
        res.test_skipped += msgs.test_skipped
        res.errors += msgs.errors
    except asyncio.CancelledError:
        raise
    except Exception as e:  # DB restarting, ...: try again on the next tick
        log.warning("outbox_delivery failed: %s: %s", type(e).__name__, str(e)[:200])
        return None
    if res.sent or res.failed:
        log.info("outbox_delivery: sent=%s failed=%s %s", res.sent, res.failed, "; ".join(res.errors)[:300])
    return res
