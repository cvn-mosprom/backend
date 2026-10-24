"""S6 HR bot: access control, commands on a seeded database, CSV, status buttons. Real Postgres (see hr_support.py),
mocked aiogram objects: nothing talks to Telegram."""

from __future__ import annotations

import ast
import csv
import io
from datetime import datetime, timezone
from pathlib import Path

import pytest
from aiogram.filters import CommandObject
from sqlalchemy import text

from app.config import Settings, parse_id_list
from app.hr import handlers, queries, runtime
from hr_support import (ADMIN, ADMIN2, NOW, STRANGER, add_lead, add_user, fake_callback, fake_message, make_settings,
                        reply_text, run_db, x)


def cmd(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


# ---------------------------------------------------------------- config

def test_settings_parse_hr_env():
    s = Settings.from_env({"HR_ADMIN_TG_IDS": "10, 20;30 x 40", "HR_CHAT_ID": "-100123", "BOTS_ENABLED": "0",
                           "DATABASE_URL": "postgresql+asyncpg://u:p@db/x", "PGBOUNCER": "1"})
    assert s.hr_admin_tg_ids == {10, 20, 30, 40} and s.hr_chat_id == -100123 and not s.bots_enabled
    assert s.pgbouncer and s.database_url.endswith("/x")
    d = Settings.from_env({})
    assert d.hr_admin_tg_ids == frozenset() and d.hr_chat_id is None and d.bots_enabled and not d.pgbouncer
    assert parse_id_list(None) == frozenset() and parse_id_list("abc,,") == frozenset()
    assert Settings.from_env({"HR_CHAT_ID": "oops"}).hr_chat_id is None


def test_funnel_steps_are_known_api_events():
    src = Path(__file__).resolve().parents[2] / "api" / "app" / "events.py"
    if not src.exists():
        pytest.skip("api sources are not next to the bot")
    names: set[str] = set()
    for node in ast.walk(ast.parse(src.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "EVENT_NAMES" for t in node.targets):
            names = {c.value for c in ast.walk(node.value) if isinstance(c, ast.Constant) and isinstance(c.value, str)}
    assert names, "EVENT_NAMES not found in api/app/events.py"
    assert {n for n, _ in queries.FUNNEL_STEPS} <= names


def test_parse_filter():
    f = queries.parse_filter("contacted РХТУ им. Менделеева #senior all")
    assert (f.status, f.tag, f.include_revoked, f.university) == ("contacted", "senior", True, "РХТУ им. Менделеева")
    assert queries.parse_filter("новый") == queries.LeadFilter(status="new")
    assert queries.parse_filter(None) == queries.LeadFilter()
    assert queries.parse_filter("high_quiz МГУПП").tag == "high_quiz"
    assert queries.like_pattern("50%_x!") == "%50!%!_x!!%"


# ---------------------------------------------------------------- access control

COMMANDS = [("on_leads", "leads"), ("on_lead", "lead"), ("on_export", "export"), ("on_funnel", None), ("on_sources", None),
            ("on_stats", None), ("on_top", "top")]


def test_non_admin_is_refused_everywhere():
    async def go(sm):
        for fn, name in COMMANDS:
            m = fake_message(STRANGER)
            f = getattr(handlers, fn)
            await (f(m, cmd(name or fn[3:], "1")) if name else f(m))
            assert reply_text(m) == handlers.DENIED, fn
            m.answer_document.assert_not_called()
        m = fake_message(STRANGER)
        await handlers.on_help(m)
        assert reply_text(m) == handlers.DENIED
        # a message without a sender (channel post) is refused as well
        m = fake_message(STRANGER)
        m.from_user = None
        await handlers.on_stats(m)
        assert reply_text(m) == handlers.DENIED

    run_db(go)


def test_admin_gets_help_and_commands_work():
    async def go(sm):
        m = fake_message(ADMIN)
        await handlers.on_help(m)
        assert "/leads" in reply_text(m) and "/export" in reply_text(m)
        m = fake_message(ADMIN2)
        await handlers.on_stats(m)
        assert "Статистика" in reply_text(m)

    run_db(go)


def test_no_database_is_reported_politely():
    async def go(sm):
        runtime.setup(make_settings(database_url=""), None, None)
        m = fake_message(ADMIN)
        await handlers.on_leads(m, cmd("leads"))
        assert reply_text(m) == handlers.NO_DB

    run_db(go, seed=False)


def test_a_failing_query_does_not_raise_into_the_dispatcher():
    async def go(sm):
        await sm.kw["bind"].dispose()
        runtime.get().sm = lambda: (_ for _ in ()).throw(RuntimeError("db is down"))  # sessionmaker that blows up
        m = fake_message(ADMIN)
        await handlers.on_funnel(m)
        assert "Не удалось" in reply_text(m)

    run_db(go)


# ---------------------------------------------------------------- /leads, /lead

def test_leads_default_excludes_revoked_and_bots_newest_first():
    async def go(sm):
        m = fake_message(ADMIN)
        await handlers.on_leads(m, cmd("leads"))
        out = reply_text(m)
        assert out.index("Петров Борис") < out.index("Иванова Анна")  # newest first
        assert "Сидорова" not in out and "Бот Ботов" not in out
        assert "· 2 ·" in out and "/lead_" in out

    run_db(go)


def test_leads_filters_and_all():
    async def go(sm):
        async def ask(args):
            m = fake_message(ADMIN)
            await handlers.on_leads(m, cmd("leads", args))
            return reply_text(m)

        assert "Петров" in await ask("contacted") and "Иванова" not in await ask("contacted")
        out = await ask("мгупп")  # case-insensitive substring of the university
        assert "Иванова" in out and "Петров" not in out and "Сидорова" not in out
        out = await ask("мгупп all")
        assert "Иванова" in out and "Сидорова" in out and "🚫" in out and "Бот Ботов" not in out
        assert "Иванова" in await ask("#senior") and "Петров" not in await ask("senior")
        assert "Петров" in await ask("ready_for_season")
        assert "Иванова" in await ask("новый")
        assert "Ничего не найдено" in await ask("hired")
        assert "Ничего не найдено" in await ask("rejected МГУПП")

    run_db(go)


def test_leads_pagination():
    async def go(sm):
        async with sm() as s, s.begin():
            uid = await x(s, "SELECT id FROM users WHERE tg_id = 11")
            for i in range(24):  # 2 + 24 = 26 leads -> 3 pages of 10
                u = await add_user(s, 500 + i, f"U{i}")
                await add_lead(s, u, 2, f"Кандидат {i:02d}", "ПГУ")
        async with sm() as s:
            rows, total = await queries.list_leads(s, queries.LeadFilter(), 0)
        assert total == 26 and len(rows) == 10
        m = fake_message(ADMIN)
        await handlers.on_leads(m, cmd("leads", "ПГУ"))
        kb = m.answer.call_args.kwargs["reply_markup"]
        buttons = [b for row in kb.inline_keyboard for b in row]
        assert [b.text for b in buttons] == ["▶"]
        cb = fake_callback(ADMIN, buttons[0].callback_data)
        await handlers.on_leads_page(cb)
        shown = cb.message.edit_text.call_args.args[0]
        assert "стр. 2/3" in shown
        kb2 = cb.message.edit_text.call_args.kwargs["reply_markup"]
        assert [b.text for b in kb2.inline_keyboard[0]] == ["◀", "▶"]
        # a stranger cannot page; an unknown key says the list is stale
        bad = fake_callback(STRANGER, buttons[0].callback_data)
        await handlers.on_leads_page(bad)
        bad.message.edit_text.assert_not_called()
        assert bad.answer.call_args.kwargs.get("show_alert")
        stale = fake_callback(ADMIN, "lp:zzzzzz:1")
        await handlers.on_leads_page(stale)
        assert "устарел" in stale.answer.call_args.args[0]
        assert uid

    run_db(go)


def test_lead_card_command_and_link():
    async def go(sm):
        lid = await _lead_id(sm, "Иванова Анна")
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(lid)))
        card = reply_text(m)
        for part in ("Иванова Анна", "+79991112233", "anna@example.com", "@anna", "МГУПП", "Пищевая инженерия", "курс: 4",
                     "Оператор линии розлива", "готов к сезону", "оплачиваемая практика", "#senior #high_quiz",
                     "Лаборант ОТК", "место в сезоне: 2", "квиз: 9/10", "Источник: ref_1", "Новый"):
            assert part in card, part
        assert "лет" in card or "год" in card
        kb = m.answer.call_args.kwargs["reply_markup"]
        assert [b.callback_data.split(":")[2] for row in kb.inline_keyboard for b in row] == [
            "new", "contacted", "interview", "rejected", "hired"]
        m = fake_message(ADMIN)
        await handlers.on_lead_link(m, _link_cmd(lid))
        assert "Иванова Анна" in reply_text(m)
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", "abc"))
        assert "Использование" in reply_text(m)
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", "424242"))
        assert "не найдена" in reply_text(m)
        # a revoked lead: no buttons, no contact data
        rid = await _lead_id(sm, "Сидорова Вера")
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(rid)))
        assert m.answer.call_args.kwargs["reply_markup"] is None
        assert "согласие отозвано" in reply_text(m) and "+79993334455" not in reply_text(m)

    run_db(go)


def _link_cmd(lid: int) -> CommandObject:
    import re

    return CommandObject(prefix="/", command=f"lead_{lid}", regexp_match=re.fullmatch(r"lead_(\d+)", f"lead_{lid}"))


async def _lead_id(sm, name: str) -> int:
    async with sm() as s:
        return await x(s, "SELECT id FROM leads WHERE full_name = :n", n=name)


# ---------------------------------------------------------------- /export

def _parse_csv(doc) -> list[dict[str, str]]:
    raw = doc.data
    assert raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM for Excel"
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))


def test_export_csv_content_and_encoding():
    async def go(sm):
        m = fake_message(ADMIN)
        await handlers.on_export(m, cmd("export"))
        doc = m.answer_document.call_args.args[0]
        assert doc.filename.startswith("leads_") and doc.filename.endswith(".csv")
        rows = _parse_csv(doc)
        assert [r["ФИО"] for r in rows] == ["Петров Борис", "Иванова Анна"]  # no revoked, no bot; newest first
        a = rows[1]
        assert a["телефон"] == "+79991112233" and a["email"] == "anna@example.com" and a["telegram"] == "anna"
        assert a["вуз"] == "МГУПП" and a["направление"] == "Пищевая инженерия" and a["курс"] == "4"
        assert a["вакансия"] == "Оператор линии розлива" and a["теги"] == "senior,high_quiz"
        assert a["лучшая профессия"] == "Лаборант ОТК" and a["место в сезоне"] == "2" and a["источник"] == "ref_1"
        assert a["дата рождения"] == "2003-05-01" and int(a["возраст"]) >= 22
        assert a["квиз (верных из 10)"] == "9" and a["статус"] == "new"
        assert rows[0]["статус"] == "contacted" and rows[0]["email"] == ""
        assert "Сидорова" not in doc.data.decode("utf-8-sig") and "Бот Ботов" not in doc.data.decode("utf-8-sig")
        assert doc.data.count(b"\r\n") == 3  # header + 2 rows

        m = fake_message(ADMIN)  # filters work, and `all` never brings revoked leads into the file
        await handlers.on_export(m, cmd("export", "contacted all"))
        assert [r["ФИО"] for r in _parse_csv(m.answer_document.call_args.args[0])] == ["Петров Борис"]
        m = fake_message(ADMIN)
        await handlers.on_export(m, cmd("export", "МГУПП all"))
        assert [r["ФИО"] for r in _parse_csv(m.answer_document.call_args.args[0])] == ["Иванова Анна"]

    run_db(go)


def test_export_empty_and_formula_injection():
    async def go(sm):
        async with sm() as s, s.begin():
            u = await add_user(s, 700, "Хакер")
            await add_lead(s, u, 2, "=HYPERLINK(\"http://evil\")", "@evil", phone="+7 (999) 111-22-33", email="-1+1@x.ru")
        m = fake_message(ADMIN)
        await handlers.on_export(m, cmd("export"))
        rows = {r["id"]: r for r in _parse_csv(m.answer_document.call_args.args[0])}
        evil = next(r for r in rows.values() if "HYPERLINK" in r["ФИО"])
        assert evil["ФИО"].startswith("'=") and evil["вуз"].startswith("'@") and evil["email"].startswith("'-")
        assert evil["телефон"] == "+7 (999) 111-22-33"  # phones keep their leading +

    run_db(go)
    csv_bytes = queries.build_csv([])
    assert csv_bytes.startswith(b"\xef\xbb\xbf") and csv_bytes.count(b"\r\n") == 1


# ---------------------------------------------------------------- /funnel /sources /stats /top

def test_funnel_counts_current_season_without_bots():
    async def go(sm):
        m = fake_message(ADMIN)
        await handlers.on_funnel(m)
        out = reply_text(m)
        assert "сезон 2" in out
        want = {"Открыли игру": 4, "Начали сессию": 2, "Запустили смену": 2, "Завершили смену": 1, "Дошли до финала": 1,
                "Открыли анкету": 2, "Отправили анкету": 1}
        for label, n in want.items():
            line = next(l for l in out.splitlines() if label in l)
            assert f"<b>{n}</b>" in line, (label, line)
        assert "50% от шага 1" in next(l for l in out.splitlines() if "Запустили смену" in l)
        assert "игроков с цехом 4" in out and "смен принято 3 (игроков 2)" in out and "анкет 2 (+1 отозвано)" in out
        async with sm() as s:
            data = await queries.funnel(s, await queries.current_season(s))
        assert [n for _, _, n in data["steps"]] == [4, 2, 2, 1, 1, 2, 1]

    run_db(go)


def test_funnel_without_seasons():
    async def go(sm):
        m = fake_message(ADMIN)
        await handlers.on_funnel(m)
        assert "Сезонов пока нет" in reply_text(m)

    run_db(go, seed=False)


def test_sources_conversion_excludes_bots_and_revoked():
    async def go(sm):
        async with sm() as s:
            rows = await queries.sources(s)
        by = {r["src"]: (r["users"], r["leads"]) for r in rows}
        assert by == {"ref_1": (2, 2), "qr_hall": (1, 0), "hr_spring": (1, 0), "(без метки)": (1, 0)}
        assert rows[0]["src"] == "ref_1"
        m = fake_message(ADMIN)
        await handlers.on_sources(m)
        out = reply_text(m)
        assert "ref_1: 2 → 2 (100%)" in out and "Всего: 5 → 2 (40%)" in out

    run_db(go)


def test_stats_numbers():
    async def go(sm):
        async with sm() as s:
            d = await queries.stats(s, 10)
        assert (d["users"], d["new_today"], d["dau"], d["factories"], d["shifts_today"]) == (5, 5, 3, 5, 2)
        assert (d["quiz_started"], d["quiz_completed"], d["quiz_avg"]) == (2, 1, 7.0)
        assert d["leads"] == {"new": 1, "contacted": 1} and d["leads_revoked"] == 1
        m = fake_message(ADMIN)
        await handlers.on_stats(m)
        out = reply_text(m)
        assert "Игроков: <b>5</b>" in out and "DAU сегодня: <b>3</b>" in out and "Цехов: 5, смен сегодня: 2" in out
        assert "Анкет: <b>2</b>" in out and "отозвано 1" in out and "прошли 1, средний результат 7.0/10" in out

    run_db(go)


def test_top_current_season_bots_marked_and_university_filter():
    async def go(sm):
        async def ask(args=None):
            m = fake_message(ADMIN)
            await handlers.on_top(m, cmd("top", args))
            return reply_text(m)

        out = await ask()
        order = ["Бот-цех (", "Цех Бориса", "Бот-цех 2", "Цех Анны", "Цех Веры", "Цех Глеба"]
        assert [out.index(x_) for x_ in order] == sorted(out.index(x_) for x_ in order)
        assert out.count("🤖") == 2 and "Дины" not in out and "⭐ 999" not in out  # previous season
        out = await ask("мгупп")
        assert "Бот-цех (" in out and "Цех Анны" in out and "Цех Веры" in out and "Бориса" not in out
        assert "вуз «мгупп»" in out
        assert "Пока никого нет" in await ask("Нет такого вуза")

        runtime.get().settings = make_settings(bots_enabled=False)  # BOTS_ENABLED=0 hides them like the api does
        out = await ask()
        assert "🤖" not in out and "Бот-цех" not in out and out.index("Цех Бориса") < out.index("Цех Анны")

    run_db(go)


# ---------------------------------------------------------------- status buttons

def test_status_button_updates_db_logs_actor_and_edits_message():
    async def go(sm):
        lid = await _lead_id(sm, "Иванова Анна")
        cb = fake_callback(ADMIN, f"ls:{lid}:interview")
        await handlers.on_status_button(cb)
        async with sm() as s:
            row = (await s.execute(text("SELECT status, hr_note, updated_at, created_at FROM leads WHERE id = :i"), {"i": lid})).one()
        assert row.status == "interview" and row.updated_at > row.created_at
        assert "@hr_user (tg 1001): new → interview" in row.hr_note
        shown = cb.message.edit_text.call_args.args[0]
        assert "Собеседование" in shown and "→ interview" in shown
        kb = cb.message.edit_text.call_args.kwargs["reply_markup"]
        assert any(b.text.startswith("• ") and "Собеседование" in b.text for r in kb.inline_keyboard for b in r)
        assert "Статус" in cb.answer.call_args.args[0]
        # same status again: nothing is written
        cb2 = fake_callback(ADMIN2, f"ls:{lid}:interview")
        await handlers.on_status_button(cb2)
        assert "уже" in cb2.answer.call_args.args[0]
        # a second change appends a second audit line
        await handlers.on_status_button(fake_callback(ADMIN2, f"ls:{lid}:hired"))
        async with sm() as s:
            note = await x(s, "SELECT hr_note FROM leads WHERE id = :i", i=lid)
        assert note.count("\n") == 1 and "interview → hired" in note

    run_db(go)


def test_status_button_refused_for_revoked_lead():
    async def go(sm):
        rid = await _lead_id(sm, "Сидорова Вера")
        before = await _snapshot(sm, rid)
        cb = fake_callback(ADMIN, f"ls:{rid}:hired")
        await handlers.on_status_button(cb)
        assert cb.answer.call_args.kwargs.get("show_alert") and "отозвал" in cb.answer.call_args.args[0]
        assert await _snapshot(sm, rid) == before  # status, hr_note, updated_at untouched
        assert cb.message.edit_text.call_args.kwargs["reply_markup"] is None  # buttons are removed

    run_db(go)


async def _snapshot(sm, lid):
    async with sm() as s:
        r = (await s.execute(text("SELECT status, hr_note, updated_at FROM leads WHERE id = :i"), {"i": lid})).one()
    return tuple(r)


def test_status_button_refused_for_non_admin_and_garbage():
    async def go(sm):
        lid = await _lead_id(sm, "Иванова Анна")
        before = await _snapshot(sm, lid)
        cb = fake_callback(STRANGER, f"ls:{lid}:hired")
        await handlers.on_status_button(cb)
        assert cb.answer.call_args.kwargs.get("show_alert") and "Нет доступа" in cb.answer.call_args.args[0]
        cb.message.edit_text.assert_not_called()
        for data, needle in ((f"ls:{lid}:bogus", "Неизвестный"), ("ls:abc:new", "Некорректная"), ("ls:99999:new", "не найдена")):
            c = fake_callback(ADMIN, data)
            await handlers.on_status_button(c)
            assert needle in c.answer.call_args.args[0], data
        assert await _snapshot(sm, lid) == before

    run_db(go)


def test_set_lead_status_outcomes():
    async def go(sm):
        lid = await _lead_id(sm, "Петров Борис")
        async with sm() as s:
            assert (await queries.set_lead_status(s, lid, "contacted", "x"))[0] == "unchanged"
        async with sm() as s:
            assert (await queries.set_lead_status(s, lid, "nope", "x")) == ("bad_status", None)
        async with sm() as s:
            out, card = await queries.set_lead_status(s, lid, "rejected", "tester", datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc))
        assert out == "ok" and card["status"] == "rejected" and "2026-01-02 03:04 UTC · tester: contacted → rejected" in card["hr_note"]

    run_db(go)
