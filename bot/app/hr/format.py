"""Text rendering for the HR bot (Telegram HTML). Every user-provided value goes through ``esc``."""

from __future__ import annotations

import html
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from typing import Any

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

MSK = timezone(timedelta(hours=3))  # Moscow has no DST: a fixed offset avoids needing tzdata in the slim image

STATUSES = ("new", "contacted", "interview", "rejected", "hired")
STATUS_LABELS = {
    "new": "🆕 Новый", "contacted": "📞 Связались", "interview": "🗓 Собеседование",
    "rejected": "❌ Отказ", "hired": "✅ Принят",
}
STATUS_ALIASES = {  # /leads новый ...
    "новый": "new", "новые": "new", "связались": "contacted", "контакт": "contacted", "собес": "interview",
    "собеседование": "interview", "отказ": "rejected", "принят": "hired", "принятые": "hired",
}
TAGS = ("senior", "profile_direction", "ready_for_season", "high_quiz")
PROFESSIONS = {  # content ProfessionId -> title (vendor/frontend professions.ts)
    "kvasovar": "Мастер-технолог (квасовар)", "operator": "Оператор линии розлива", "adjuster": "Наладчик оборудования",
    "lab": "Лаборант ОТК", "storekeeper": "Кладовщик", "forklift": "Водитель погрузчика", "kipia": "Инженер КИПиА",
}
READINESS = {"now": "готов выйти сразу", "season": "готов к сезону", "later": "позже"}
PRACTICE = {"paid": "оплачиваемая практика", "free": "неоплачиваемая практика", "job": "работа"}
INSTITUTION_KINDS = {"vuz": "вуз", "college": "колледж"}  # leads.institution_kind
# The quiz bank of api/app/data/quiz_bank.json (= the frontend's): the bot has no api import, a test in api/tests keeps
# these two in sync. Answers to other ids (the S5 bank, q1978...) are not counted.
QUIZ_TOTAL = 8
QUIZ_QUESTION_IDS = ("q_year", "q_olymp", "q_kvas", "q_izba", "q_white", "q_cities", "q_fields", "q_space")

LEAD_PREFIX = "ls"  # callback_data: ls:<lead_id>:<status>


def esc(v: Any) -> str:
    return html.escape(str(v), quote=False)


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def age_years(born: date | None, today: date) -> int | None:
    if born is None:
        return None
    return today.year - born.year - ((today.month, today.day) < (born.month, born.day))


def fmt_dt(dt: datetime | None) -> str:
    return dt.astimezone(MSK).strftime("%d.%m.%Y %H:%M") if dt else "-"


def fmt_date(d: date | datetime | None) -> str:
    return d.strftime("%d.%m.%Y") if d else "-"


def status_label(status: str) -> str:
    return STATUS_LABELS.get(status, status)


def card_from_payload(p: Mapping[str, Any]) -> dict[str, Any]:
    """Fallback card built from the outbox payload (the lead row is gone / unreadable)."""
    created = p.get("created_at")
    try:
        created_at = datetime.fromisoformat(str(created).replace("Z", "+00:00")) if created else None
    except ValueError:
        created_at = None
    return {
        "id": p.get("lead_id"), "full_name": p.get("full_name") or "-", "birth_date": None, "phone": p.get("phone"),
        "email": p.get("email"), "telegram": p.get("telegram"), "university": p.get("university") or "-",
        "institution_kind": p.get("institution_kind"), "direction": p.get("direction"), "course": p.get("course"), "vacancy": p.get("vacancy"), "ready_from": None,
        "readiness": p.get("readiness"), "practice_type": p.get("practice_type"), "tags": list(p.get("tags") or []),
        "status": "new", "hr_note": None, "best_profession": p.get("best_profession"),
        "season_place": p.get("season_place"), "quiz_score": p.get("quiz_score"), "consent_revoked_at": None,
        "source": p.get("source"), "tg_id": p.get("tg_id"), "created_at": created_at, "tg_username": None,
    }


def render_card(c: Mapping[str, Any], *, new: bool = False, today: date | None = None) -> str:
    today = today or datetime.now(MSK).date()
    lid = c.get("id")
    if c.get("consent_revoked_at"):
        return (f"🚫 <b>Анкета #{lid}: согласие отозвано</b> ({fmt_dt(c['consent_revoked_at'])})\n"
                f"{esc(c.get('full_name') or '-')}, {esc(c.get('university') or '-')}\n"
                "Кандидат отозвал согласие на обработку персональных данных (152-ФЗ): "
                "не связывайтесь с ним и не используйте его данные. Статус не меняется.")
    lines = [f"{'🆕 Новая анкета' if new else '📋 Анкета'} <b>#{lid}</b>"]
    age = age_years(c.get("birth_date"), today)
    who = f"<b>{esc(c.get('full_name') or '-')}</b>"
    if age is not None:
        who += f", {age} {plural(age, 'год', 'года', 'лет')}"
    lines.append(who)
    if c.get("phone"):
        lines.append(f"📞 {esc(c['phone'])}")
    if c.get("email"):
        lines.append(f"✉️ {esc(c['email'])}")
    if c.get("telegram"):
        lines.append(f"✈️ @{esc(str(c['telegram']).lstrip('@'))}")
    study = [esc(c.get("university") or "-")]
    if c.get("institution_kind"):
        study.append(esc(INSTITUTION_KINDS.get(c["institution_kind"], c["institution_kind"])))
    if c.get("course"):
        study.append(f"курс: {esc(c['course'])}")
    lines.append("🎓 " + " · ".join(study))
    if c.get("direction"):  # the form's `department` (stored as its label) or the free text of an older form
        lines.append(f"🧭 Направление: {esc(c['direction'])}")
    if c.get("vacancy"):
        lines.append(f"💼 Вакансия: {esc(PROFESSIONS.get(c['vacancy'], c['vacancy']))}")
    ready = READINESS.get(c.get("readiness") or "", c.get("readiness"))
    if ready or c.get("ready_from"):
        lines.append("⏱ Готовность: " + ", ".join(
            x for x in (esc(ready) if ready else "", f"с {fmt_date(c['ready_from'])}" if c.get("ready_from") else "") if x))
    if c.get("practice_type"):
        lines.append(f"📋 Тип: {esc(PRACTICE.get(c['practice_type'], c['practice_type']))}")
    if c.get("tags"):
        lines.append("🏷 " + " ".join("#" + esc(t) for t in c["tags"]))
    game = []
    if c.get("best_profession"):
        game.append("лучшая профессия: " + esc(PROFESSIONS.get(c["best_profession"], c["best_profession"])))
    if c.get("season_place") is not None:
        game.append(f"место в сезоне: {c['season_place']}")
    if c.get("quiz_score") is not None:
        game.append(f"квиз: {c['quiz_score']}/{QUIZ_TOTAL}")
    if game:
        lines.append("🎮 " + ", ".join(game))
    lines.append(f"📍 Источник: {esc(c['source']) if c.get('source') else 'прямой заход'}")
    lines.append(f"Статус: <b>{status_label(c.get('status') or 'new')}</b>")
    notes = [n for n in (c.get("hr_note") or "").splitlines() if n.strip()][-3:]
    if notes:
        lines.append("<i>" + "\n".join(esc(n) for n in notes) + "</i>")
    return "\n".join(lines)


def render_revoked_notice(c: Mapping[str, Any]) -> str:
    return (f"🚫 <b>Отзыв согласия</b>: анкета #{c.get('id')} ({esc(c.get('full_name') or '-')}, "
            f"{esc(c.get('university') or '-')}).\nКандидат отозвал согласие на обработку данных (152-ФЗ): "
            "прекратите обработку и не связывайтесь с ним.")


def status_keyboard(lead_id: int, current: str | None) -> InlineKeyboardMarkup:
    def btn(s: str) -> InlineKeyboardButton:
        mark = "• " if s == current else ""
        return InlineKeyboardButton(text=mark + status_label(s), callback_data=f"{LEAD_PREFIX}:{lead_id}:{s}")

    return InlineKeyboardMarkup(inline_keyboard=[[btn("new"), btn("contacted"), btn("interview")],
                                                 [btn("rejected"), btn("hired")]])


def card_keyboard(c: Mapping[str, Any]) -> InlineKeyboardMarkup | None:
    """Status buttons, none for a revoked lead."""
    if c.get("consent_revoked_at") or c.get("id") is None:
        return None
    return status_keyboard(int(c["id"]), c.get("status"))


# ---------------------------------------------------------------- lists

def render_leads_page(rows: Sequence[Mapping[str, Any]], total: int, page: int, pages: int, title: str) -> str:
    if not rows:
        return f"<b>{esc(title)}</b>\nНичего не найдено."
    out = [f"<b>{esc(title)}</b> · {total} · стр. {page + 1}/{pages}"]
    for r in rows:
        tags = (" " + " ".join("#" + esc(t) for t in r["tags"])) if r.get("tags") else ""
        rev = " 🚫" if r.get("consent_revoked_at") else ""
        out.append(f"/lead_{r['id']} {esc(r['full_name'])} · {esc(r['university'])} · "
                   f"{STATUS_LABELS.get(r['status'], r['status'])}{rev}{tags} · {r['created_at'].astimezone(MSK):%d.%m}")
    return "\n".join(out)


def pct(part: int, whole: int) -> str:
    return f"{round(100 * part / whole)}%" if whole else "-"


# ---------------------------------------------------------------- analytics

def render_funnel(season: Mapping[str, Any], data: Mapping[str, Any]) -> str:
    steps = data["steps"]
    first = steps[0][2]
    out = [f"<b>📊 Воронка · сезон {season['id']}</b> (с {fmt_dt(season['starts_at'])} МСК)"]
    prev = None
    for i, (_, label, n) in enumerate(steps, 1):
        extra = f" · {pct(n, first)} от шага 1" if i > 1 else ""
        extra += f" · {pct(n, prev)} от пред." if i > 2 and prev is not None else ""
        out.append(f"{i}. {esc(label)}: <b>{n}</b>{extra}")
        prev = n
    t = data["tables"]
    out.append(f"\nПо таблицам: игроков с цехом {t['players']}, смен принято {t['shifts']} "
               f"(игроков {t['shift_players']}), анкет {t['leads']}"
               + (f" (+{t['revoked']} отозвано)" if t["revoked"] else ""))
    out.append("Уникальные игроки, демо-боты исключены.")
    return "\n".join(out)


def render_sources(rows: Sequence[Mapping[str, Any]]) -> str:
    if not rows:
        return "<b>📍 Источники</b>\nПока нет пользователей."
    total_u = sum(int(r["users"]) for r in rows)
    total_l = sum(int(r["leads"]) for r in rows)
    out = ["<b>📍 Источники</b> (start_param → игроки → анкеты)"]
    for r in rows:
        out.append(f"{esc(r['src'])}: {r['users']} → {r['leads']} ({pct(int(r['leads']), int(r['users']))})")
    out.append(f"Всего: {total_u} → {total_l} ({pct(total_l, total_u)}). Демо-боты исключены, отозванные анкеты не считаются.")
    return "\n".join(out)


def render_stats(d: Mapping[str, Any], season: Mapping[str, Any] | None) -> str:
    leads = d["leads"]
    by_status = ", ".join(f"{STATUS_LABELS[s]} {leads.get(s, 0)}" for s in STATUSES)
    total = sum(leads.values())
    out = [
        "<b>📈 Статистика</b>" + (f" · сезон {season['id']}" if season else ""),
        f"Игроков: <b>{d['users']}</b> (новых сегодня {d['new_today']}), DAU сегодня: <b>{d['dau']}</b>",
        f"Цехов: {d['factories']}, смен сегодня: {d['shifts_today']}",
        f"Анкет: <b>{total}</b> — {by_status}" + (f"; отозвано {d['leads_revoked']}" if d["leads_revoked"] else ""),
        f"Квиз: начали {d['quiz_started']}, прошли {d['quiz_completed']}"
        + (f", средний результат {d['quiz_avg']:.1f}/{QUIZ_TOTAL}" if d["quiz_completed"] else ""),
        "«Сегодня» по Москве, демо-боты исключены.",
    ]
    return "\n".join(out)


def render_top(rows: Sequence[Mapping[str, Any]], season_id: int, university: str | None) -> str:
    head = f"<b>🏆 Топ-10 · сезон {season_id}</b>" + (f" · вуз «{esc(university)}»" if university else "")
    if not rows:
        return head + "\nПока никого нет."
    out = [head]
    for i, r in enumerate(rows, 1):
        who = r.get("first_name") or (f"@{r['username']}" if r.get("username") else f"id {r['user_id']}")
        bot = "🤖 " if r.get("is_bot") else ""
        uni = f" · {esc(r['university'])}" if r.get("university") else ""
        out.append(f"{i}. {bot}{esc(r['name'])} ({esc(who)}){uni} — ⭐ {r['stars']}")
    return "\n".join(out)
