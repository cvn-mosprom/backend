"""HR commands and callbacks. Only active ``hr_users`` (C3; ``HR_ADMIN_TG_IDS`` while that table is empty) may use them,
everybody else gets a polite refusal. Per command: aggregates = any role, candidates / status buttons = hr+, /export = hr_lead+.

/hr (help)  /leads [status] [uni] [tag] [all]  /lead <id>  /export  /funnel  /sources  /stats  /top [uni]
Callbacks: ``ls:<lead_id>:<status>`` (status buttons of a card), ``lp:<key>:<page>`` (pages of /leads).
"""

from __future__ import annotations

import logging
import re
from collections import OrderedDict
from datetime import datetime
from typing import Any

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from . import access, queries, runtime
from .format import (LEAD_PREFIX, MSK, QUIZ_TOTAL, STATUSES, card_keyboard, render_card, render_funnel,
                     render_leads_page, render_sources, render_stats, render_top, status_label)

log = logging.getLogger(__name__)
router = Router(name="hr")

DENIED = "Эта команда доступна только сотрудникам HR."
DENIED_CB = "Нет доступа: кнопки доступны только сотрудникам HR."
NO_DB = "База данных недоступна, попробуйте позже."
# (minimum role, line): /hr shows only what the caller's role may use
HELP_LINES = (
    ("analyst", "<b>HR-бот</b>"),
    ("hr", "/leads [статус] [вуз] [#тег] [all] — анкеты, новые сверху (статусы: new contacted interview rejected hired; "
           "all — с отозванными)"),
    ("hr", "/lead &lt;id&gt; — карточка анкеты"),
    ("hr_lead", "/export [статус] [вуз] [#тег] — CSV (UTF-8 с BOM, разделитель «;»)"),
    ("analyst", "/funnel — воронка текущего сезона"),
    ("analyst", "/sources — источники (start_param) и конверсия в анкету"),
    ("analyst", "/stats — сводка"),
    ("analyst", "/top [вуз] — топ-10 сезона по звёздам"),
)


def help_for(role: str | None) -> str:
    return "\n".join(line for need, line in HELP_LINES if access.at_least(role, need))


def denied_role(min_role: str, role: str | None) -> str:
    return f"Недостаточно прав: команда доступна ролям {min_role} и выше (ваша роль: {role})."

_filters: OrderedDict[str, queries.LeadFilter] = OrderedDict()  # pagination state: short key -> filter (callback_data is 64 bytes)
_FILTERS_MAX = 200


def _remember(f: queries.LeadFilter) -> str:
    key = format(abs(hash((f.status, f.university, f.tag, f.include_revoked))) % (36 ** 6), "x")
    _filters[key] = f
    _filters.move_to_end(key)
    while len(_filters) > _FILTERS_MAX:
        _filters.popitem(last=False)
    return key


def _pager(key: str, page: int, pages: int) -> InlineKeyboardMarkup | None:
    row = []
    if page > 0:
        row.append(InlineKeyboardButton(text="◀", callback_data=f"lp:{key}:{page - 1}"))
    if page + 1 < pages:
        row.append(InlineKeyboardButton(text="▶", callback_data=f"lp:{key}:{page + 1}"))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def _guard(message: Message, min_role: str = "analyst") -> runtime.Runtime | None:
    """The runtime if the sender has an active HR role of at least ``min_role`` and the database is up; otherwise answers
    and returns None."""
    rt = runtime.get()
    uid = message.from_user.id if message.from_user else None
    role = await rt.role(uid) if rt is not None else None
    if role is None:
        await message.answer(DENIED)
        return None
    if not access.at_least(role, min_role):
        await message.answer(denied_role(min_role, role))
        return None
    if rt.sm is None:
        await message.answer(NO_DB)
        return None
    return rt


async def _safe(message: Message, coro_fn) -> None:
    """Run a command body; a DB failure becomes a polite message, never an exception into the dispatcher."""
    try:
        await coro_fn()
    except Exception as e:
        log.warning("hr command failed: %s: %s", type(e).__name__, str(e)[:200])
        await message.answer("Не удалось получить данные, попробуйте ещё раз.")


@router.message(Command("hr", "hrhelp"))
async def on_help(message: Message) -> None:
    rt = runtime.get()
    uid = message.from_user.id if message.from_user else None
    role = await rt.role(uid) if rt is not None else None
    await message.answer(help_for(role) if role is not None else DENIED)


async def _send_leads_page(target: Message, rt: runtime.Runtime, f: queries.LeadFilter, page: int, *, edit: bool) -> None:
    async with rt.sm() as s:
        rows, total = await queries.list_leads(s, f, page)
    pages = max(1, -(-total // queries.PAGE_SIZE))
    if page >= pages and total:
        page = pages - 1
        async with rt.sm() as s:
            rows, total = await queries.list_leads(s, f, page)
    text_ = render_leads_page(rows, total, page, pages, f.title())
    kb = _pager(_remember(f), page, pages)
    if edit:
        try:
            await target.edit_text(text_, reply_markup=kb)
        except TelegramBadRequest:
            pass  # "message is not modified"
    else:
        await target.answer(text_, reply_markup=kb)


@router.message(Command("leads"))
async def on_leads(message: Message, command: CommandObject) -> None:
    rt = await _guard(message, "hr")
    if rt:
        await _safe(message, lambda: _send_leads_page(message, rt, queries.parse_filter(command.args), 0, edit=False))


@router.callback_query(F.data.startswith("lp:"))
async def on_leads_page(cb: CallbackQuery) -> None:
    rt = await _cb_guard(cb, "hr")
    if rt is None:
        return
    try:
        _, key, page = (cb.data or "").split(":")
        f, page_n = _filters.get(key), int(page)
    except ValueError:
        f, page_n = None, 0
    if f is None or rt.sm is None or cb.message is None:
        await cb.answer("Список устарел, повторите /leads.", show_alert=True)
        return
    try:
        await _send_leads_page(cb.message, rt, f, page_n, edit=True)
    except Exception as e:
        log.warning("leads page failed: %s: %s", type(e).__name__, str(e)[:200])
        await cb.answer("Не удалось получить данные.", show_alert=True)
        return
    await cb.answer()


async def _cb_guard(cb: CallbackQuery, min_role: str) -> runtime.Runtime | None:
    """Callback twin of ``_guard`` (an alert instead of a message; the database check stays with the caller)."""
    rt = runtime.get()
    role = await rt.role(cb.from_user.id) if rt is not None else None
    if role is None:
        await cb.answer(DENIED_CB, show_alert=True)
        return None
    if not access.at_least(role, min_role):
        await cb.answer(denied_role(min_role, role), show_alert=True)
        return None
    return rt


async def _send_lead(message: Message, rt: runtime.Runtime, lead_id: int, viewer: int | None = None) -> None:
    async with rt.sm() as s:
        card = await queries.fetch_lead(s, lead_id)
    if card is None:
        await message.answer(f"Анкета #{lead_id} не найдена.")
        return
    await queries.record_lead_event(rt.sm, lead_id, viewer, "view", {"via": "bot"})  # access log of the personal data
    await message.answer(render_card(card), reply_markup=card_keyboard(card))


@router.message(Command("lead"))
async def on_lead(message: Message, command: CommandObject) -> None:
    rt = await _guard(message, "hr")
    if not rt:
        return
    m = re.fullmatch(r"#?(\d{1,18})", (command.args or "").strip())
    if not m:
        await message.answer("Использование: /lead &lt;id&gt;")
        return
    await _safe(message, lambda: _send_lead(message, rt, int(m.group(1)), message.from_user.id))


@router.message(Command(re.compile(r"lead_(\d{1,18})")))
async def on_lead_link(message: Message, command: CommandObject) -> None:
    rt = await _guard(message, "hr")
    if rt and command.regexp_match:
        await _safe(message, lambda: _send_lead(message, rt, int(command.regexp_match.group(1)), message.from_user.id))


@router.callback_query(F.data.startswith(LEAD_PREFIX + ":"))
async def on_status_button(cb: CallbackQuery) -> None:
    rt = await _cb_guard(cb, "hr")
    if rt is None:
        return
    try:
        _, lead_id, status = (cb.data or "").split(":")
        lead_id_n = int(lead_id)
    except ValueError:
        await cb.answer("Некорректная кнопка.", show_alert=True)
        return
    if status not in STATUSES:
        await cb.answer("Неизвестный статус.", show_alert=True)
        return
    if rt.sm is None:
        await cb.answer(NO_DB, show_alert=True)
        return
    actor = queries.actor_label(cb.from_user.id, cb.from_user.username, cb.from_user.first_name)
    try:
        async with rt.sm() as s:
            outcome, card = await queries.set_lead_status(s, lead_id_n, status, actor, actor_tg_id=cb.from_user.id)
    except Exception as e:
        log.warning("status update failed: %s: %s", type(e).__name__, str(e)[:200])
        await cb.answer("Не удалось сохранить статус, попробуйте ещё раз.", show_alert=True)
        return
    if outcome == "not_found":
        await cb.answer("Анкета не найдена.", show_alert=True)
        return
    if outcome == "revoked":
        await cb.answer("Кандидат отозвал согласие: статус менять нельзя.", show_alert=True)
    elif outcome == "unchanged":
        await cb.answer(f"Статус уже «{status_label(status)}».")
    else:
        await cb.answer(f"Статус: {status_label(status)}")
    if card is not None and hasattr(cb.message, "edit_text"):  # redraw from the DB (also removes the buttons of a revoked lead)
        try:
            await cb.message.edit_text(render_card(card, new=False), reply_markup=card_keyboard(card))
        except TelegramBadRequest:
            pass  # not modified


@router.message(Command("export"))
async def on_export(message: Message, command: CommandObject) -> None:
    rt = await _guard(message, "hr_lead")
    if not rt:
        return

    async def body() -> None:
        f = queries.parse_filter(command.args)
        async with rt.sm() as s:
            rows = await queries.export_rows(s, f)
        data = queries.build_csv(rows)
        name = f"leads_{datetime.now(MSK):%Y%m%d_%H%M}.csv"
        await message.answer_document(
            BufferedInputFile(data, filename=name),
            caption=f"Анкеты: {len(rows)} (без отозванных и демо-ботов). UTF-8 с BOM, разделитель «;» — открывается в Excel.")

    await _safe(message, body)


@router.message(Command("funnel"))
async def on_funnel(message: Message) -> None:
    rt = await _guard(message)
    if not rt:
        return

    async def body() -> None:
        async with rt.sm() as s:
            season = await queries.current_season(s)
            if season is None:
                await message.answer("Сезонов пока нет.")
                return
            data = await queries.funnel(s, season)
        await message.answer(render_funnel(season, data))

    await _safe(message, body)


@router.message(Command("sources"))
async def on_sources(message: Message) -> None:
    rt = await _guard(message)
    if not rt:
        return

    async def body() -> None:
        async with rt.sm() as s:
            rows = await queries.sources(s)
        await message.answer(render_sources(rows))

    await _safe(message, body)


@router.message(Command("stats"))
async def on_stats(message: Message) -> None:
    rt = await _guard(message)
    if not rt:
        return

    async def body() -> None:
        async with rt.sm() as s:
            season = await queries.current_season(s)
            data = await queries.stats(s, QUIZ_TOTAL)
        await message.answer(render_stats(data, season))

    await _safe(message, body)


@router.message(Command("top"))
async def on_top(message: Message, command: CommandObject) -> None:
    rt = await _guard(message)
    if not rt:
        return

    async def body() -> None:
        uni = (command.args or "").strip() or None
        async with rt.sm() as s:
            season = await queries.current_season(s)
            if season is None:
                await message.answer("Сезонов пока нет.")
                return
            rows = await queries.top(s, season["id"], uni, include_bots=rt.settings.bots_enabled)
        await message.answer(render_top(rows, season["id"], uni))

    await _safe(message, body)
