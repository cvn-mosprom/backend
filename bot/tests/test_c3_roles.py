"""C3: the HR bot reads its access from the api's `hr_users` table (roles hr / hr_lead / analyst / admin), HR_ADMIN_TG_IDS is only the
fallback while the table is empty or unreadable; lead cards go to active hr / hr_lead / admin; status / view events land in
`lead_events`. Real Postgres (hr_support.py), mocked aiogram objects."""

from __future__ import annotations

import json
from aiogram.filters import CommandObject
from sqlalchemy import text

from app.hr import access, handlers, queries, runtime
from hr_support import (ADMIN, ADMIN2, STRANGER, fake_callback, fake_message, make_bot, make_settings, reply_text, run_db, x)
from app.hr import outbox

HR, LEAD, ANALYST, BOSS, FIRED = 2001, 2002, 2003, 2004, 2005
ROLE_TG = {"hr": HR, "hr_lead": LEAD, "analyst": ANALYST, "admin": BOSS}


def cmd(name: str, args: str | None = None) -> CommandObject:
    return CommandObject(prefix="/", command=name, args=args)


async def seed_roles(sm, *, fired: bool = True) -> dict[str, int]:
    ids = {}
    async with sm() as s, s.begin():
        for role, tg in ROLE_TG.items():
            ids[role] = await x(s, "INSERT INTO hr_users (tg_id, role) VALUES (:tg, :r) RETURNING id", tg=tg, r=role)
        if fired:
            await x(s, "INSERT INTO hr_users (tg_id, role, active) VALUES (:tg, 'admin', false)", tg=FIRED)
    return ids


async def lead_id(sm, name: str = "Иванова Анна") -> int:
    async with sm() as s:
        return await x(s, "SELECT id FROM leads WHERE full_name = :n", n=name)


async def events(sm, kind: str | None = None) -> list:
    async with sm() as s:
        rows = (await s.execute(text("SELECT lead_id, actor_hr_id, kind, payload FROM lead_events ORDER BY id"))).all()
    return [r for r in rows if kind is None or r.kind == kind]


# ---------------------------------------------------------------- role lookup, fallback

def test_role_comes_from_hr_users_and_inactive_or_unknown_is_none():
    async def go(sm):
        await seed_roles(sm)
        rt = runtime.get()
        assert {r: await rt.role(tg) for r, tg in ROLE_TG.items()} == {r: r for r in ROLE_TG}
        assert await rt.role(FIRED) is None and await rt.role(STRANGER) is None and await rt.role(None) is None
        # the table is not empty: HR_ADMIN_TG_IDS (ADMIN, ADMIN2 in the test settings) no longer counts
        assert await rt.role(ADMIN) is None and await rt.role(ADMIN2) is None

    run_db(go)


def test_env_list_is_the_fallback_while_the_table_is_empty_or_unreadable():
    async def go(sm):
        rt = runtime.get()
        assert await rt.role(ADMIN) == "admin" and await rt.role(STRANGER) is None  # empty table
        await seed_roles(sm, fired=False)
        assert await rt.role(ADMIN) is None
        rt.sm = lambda: (_ for _ in ()).throw(RuntimeError("db is down"))  # unreadable
        assert await rt.role(ADMIN) == "admin" and await rt.role(BOSS) is None and await rt.role(STRANGER) is None
        rt.sm = None  # no DATABASE_URL at all
        assert await rt.role(ADMIN2) == "admin"

    run_db(go)


def test_missing_table_falls_back_instead_of_raising():
    async def go(sm):
        async with sm() as s, s.begin():
            await s.execute(text("ALTER TABLE hr_users RENAME TO hr_users_gone"))
        try:
            rt = runtime.get()
            assert await rt.role(ADMIN) == "admin" and await rt.role(STRANGER) is None
            assert await rt.recipients() == sorted({ADMIN, ADMIN2}) and await rt.admin_ids() == {ADMIN, ADMIN2}
        finally:
            async with sm() as s, s.begin():
                await s.execute(text("ALTER TABLE hr_users_gone RENAME TO hr_users"))

    run_db(go)


def test_at_least_order():
    order = ["analyst", "hr", "hr_lead", "admin"]
    for i, role in enumerate(order):
        for j, need in enumerate(order):
            assert access.at_least(role, need) == (i >= j)
    assert not access.at_least(None, "analyst") and not access.at_least("ghost", "analyst")


# ---------------------------------------------------------------- command matrix

AGGREGATES = [("on_funnel", None), ("on_sources", None), ("on_stats", None), ("on_top", "top")]


async def call(name: str, tg: int, args: str | None = None):
    m = fake_message(tg)
    f = getattr(handlers, name)
    await (f(m, cmd(name[3:], args)) if name in ("on_leads", "on_lead", "on_export", "on_top") else f(m))
    return m


def denied_for_role(m) -> bool:
    return m.answer.called and reply_text(m).startswith("Недостаточно прав")


def test_aggregate_commands_are_open_to_every_active_role():
    async def go(sm):
        await seed_roles(sm)
        for role, tg in ROLE_TG.items():
            for fn, _ in AGGREGATES:
                m = await call(fn, tg, "1" if fn == "on_top" else None)
                assert reply_text(m) not in (handlers.DENIED,) and not denied_for_role(m), (role, fn)
        for tg in (FIRED, STRANGER, ADMIN):
            for fn, _ in AGGREGATES:
                assert reply_text(await call(fn, tg)) == handlers.DENIED, (tg, fn)

    run_db(go)


def test_candidate_commands_need_hr_the_analyst_gets_no_personal_data():
    async def go(sm):
        await seed_roles(sm)
        for fn, args in (("on_leads", None), ("on_lead", "1")):
            m = await call(fn, ANALYST, args)
            assert denied_for_role(m) and "analyst" in reply_text(m), fn
            assert "Иванова" not in reply_text(m) and "+7999" not in reply_text(m)
            for role in ("hr", "hr_lead", "admin"):
                m = await call(fn, ROLE_TG[role], args)
                assert not denied_for_role(m) and reply_text(m) != handlers.DENIED, (fn, role)
        # the /lead_<id> link and the pager callback follow the same rule
        lid = await lead_id(sm)
        m = fake_message(ANALYST)
        m.answer.reset_mock()
        from types import SimpleNamespace
        await handlers.on_lead_link(m, SimpleNamespace(regexp_match=SimpleNamespace(group=lambda i: str(lid))))
        assert denied_for_role(m)
        cb = fake_callback(ANALYST, "lp:abc:0")
        await handlers.on_leads_page(cb)
        assert cb.answer.call_args.kwargs["show_alert"] and "Недостаточно прав" in cb.answer.call_args.args[0]
        cb = fake_callback(FIRED, "lp:abc:0")
        await handlers.on_leads_page(cb)
        assert handlers.DENIED_CB in cb.answer.call_args.args[0]

    run_db(go)


def test_export_needs_hr_lead():
    async def go(sm):
        await seed_roles(sm)
        for role in ("hr", "analyst"):
            m = await call("on_export", ROLE_TG[role])
            assert denied_for_role(m) and "hr_lead" in reply_text(m), role
            m.answer_document.assert_not_called()
        for role in ("hr_lead", "admin"):
            m = await call("on_export", ROLE_TG[role])
            m.answer_document.assert_awaited_once()
        for tg in (FIRED, STRANGER):
            m = await call("on_export", tg)
            assert reply_text(m) == handlers.DENIED
            m.answer_document.assert_not_called()

    run_db(go)


def test_help_lists_only_what_the_role_may_use():
    async def go(sm):
        await seed_roles(sm)
        texts = {}
        for role, tg in ROLE_TG.items():
            m = fake_message(tg)
            await handlers.on_help(m)
            texts[role] = reply_text(m)
        assert "/funnel" in texts["analyst"] and "/leads" not in texts["analyst"] and "/export" not in texts["analyst"]
        assert "/leads" in texts["hr"] and "/lead " in texts["hr"] and "/export" not in texts["hr"]
        assert "/export" in texts["hr_lead"] and "/export" in texts["admin"]
        m = fake_message(STRANGER)
        await handlers.on_help(m)
        assert reply_text(m) == handlers.DENIED

    run_db(go)


# ---------------------------------------------------------------- status buttons and lead events

def test_status_button_needs_hr_and_writes_a_lead_event():
    async def go(sm):
        ids = await seed_roles(sm)
        lid = await lead_id(sm)
        cb = fake_callback(ANALYST, f"ls:{lid}:interview")
        await handlers.on_status_button(cb)
        assert cb.answer.call_args.kwargs["show_alert"] and "Недостаточно прав" in cb.answer.call_args.args[0]
        cb.message.edit_text.assert_not_called()
        async with sm() as s:
            assert await x(s, "SELECT status FROM leads WHERE id = :i", i=lid) == "new"
        assert await events(sm) == []
        cb = fake_callback(FIRED, f"ls:{lid}:interview")
        await handlers.on_status_button(cb)
        assert "Нет доступа" in cb.answer.call_args.args[0] and await events(sm) == []

        cb = fake_callback(HR, f"ls:{lid}:interview")
        await handlers.on_status_button(cb)
        assert "Статус" in cb.answer.call_args.args[0]
        await handlers.on_status_button(fake_callback(LEAD, f"ls:{lid}:interview"))  # unchanged: no event
        await handlers.on_status_button(fake_callback(BOSS, f"ls:{lid}:hired"))
        ev = await events(sm, "status")
        assert [(e.lead_id, e.actor_hr_id) for e in ev] == [(lid, ids["hr"]), (lid, ids["admin"])]
        assert ev[0].payload == {"from": "new", "to": "interview", "via": "bot"}
        assert ev[1].payload == {"from": "interview", "to": "hired", "via": "bot"}

    run_db(go)


def test_status_event_without_an_hr_row_has_no_actor_and_a_failing_audit_never_blocks_the_status():
    async def go(sm):  # env fallback: the table is empty, ADMIN acts
        lid = await lead_id(sm)
        await handlers.on_status_button(fake_callback(ADMIN, f"ls:{lid}:contacted"))
        ev = await events(sm, "status")
        assert len(ev) == 1 and ev[0].actor_hr_id is None
        async with sm() as s, s.begin():
            await s.execute(text("ALTER TABLE lead_events RENAME TO lead_events_gone"))
        try:
            cb = fake_callback(ADMIN, f"ls:{lid}:hired")
            await handlers.on_status_button(cb)
            async with sm() as s:
                assert await x(s, "SELECT status FROM leads WHERE id = :i", i=lid) == "hired"
        finally:
            async with sm() as s, s.begin():
                await s.execute(text("ALTER TABLE lead_events_gone RENAME TO lead_events"))

    run_db(go)


def test_lead_card_view_is_logged_with_the_actor():
    async def go(sm):
        ids = await seed_roles(sm)
        lid = await lead_id(sm)
        m = await call("on_lead", HR, str(lid))
        assert "Иванова" in reply_text(m)
        await call("on_lead", BOSS, f"#{lid}")
        await call("on_lead", HR, "99999")  # unknown lead: nothing to log
        await call("on_lead", ANALYST, str(lid))  # refused: nothing logged
        ev = await events(sm, "view")
        assert [(e.lead_id, e.actor_hr_id, e.payload) for e in ev] == [(lid, ids["hr"], {"via": "bot"}),
                                                                      (lid, ids["admin"], {"via": "bot"})]

    run_db(go)


def test_record_lead_event_is_best_effort():
    async def go(sm):
        await queries.record_lead_event(sm, 987654, None, "view", {})  # no such lead: logged, not raised
        assert await events(sm) == []

    run_db(go)


# ---------------------------------------------------------------- recipients and the 5xx alert

def test_lead_cards_go_to_active_card_roles():
    async def go(sm):
        rt = runtime.get()
        assert await rt.recipients() == sorted({ADMIN, ADMIN2})  # empty table: env
        await seed_roles(sm)
        assert await rt.recipients() == sorted([HR, LEAD, BOSS])  # no analyst (personal data), no deactivated admin
        rt.settings = make_settings(hr_chat_id=-100500)
        assert await rt.recipients() == [-100500]  # HR_CHAT_ID wins
        assert await rt.admin_ids() == {BOSS}

    run_db(go)


def test_only_analysts_in_the_table_means_no_card_recipients():
    async def go(sm):
        async with sm() as s, s.begin():
            await s.execute(text("INSERT INTO hr_users (tg_id, role) VALUES (:t, 'analyst')"), {"t": ANALYST})
        rt = runtime.get()
        assert await rt.recipients() == [] and await rt.admin_ids() == frozenset()

    run_db(go)


def test_outbox_delivers_to_hr_users(monkeypatch):
    bot = make_bot()

    async def go(sm):
        await seed_roles(sm)
        async with sm() as s, s.begin():
            lid = await x(s, "SELECT id FROM leads WHERE full_name = 'Иванова Анна'")
            await s.execute(text("INSERT INTO outbox (kind, payload) VALUES ('lead_created', CAST(:p AS jsonb))"),
                            {"p": json.dumps({"lead_id": lid, "full_name": "Иванова Анна"})})
        res = await outbox.outbox_delivery()
        assert res.sent == 1
        sent_to = sorted(c.args[0] for c in bot.send_message.call_args_list)
        assert sent_to == sorted([HR, LEAD, BOSS])

    run_db(go, bot=bot)
