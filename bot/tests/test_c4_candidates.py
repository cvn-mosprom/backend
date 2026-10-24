"""C4 in the bot: the new pipeline statuses on the lead card buttons (and the old `contacted` button), version / stage bump,
and the delivery of ``hr_message`` outbox rows (HR writes to a candidate from the CRM) to the candidate's own chat."""

from __future__ import annotations

import json
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError
from aiogram.methods import SendMessage
from sqlalchemy import text

from app.hr import handlers, outbox, queries, runtime
from app.hr.format import STATUS_LABELS, STATUSES, status_keyboard
from hr_support import ADMIN, ADMIN2, fake_callback, make_bot, make_settings, run_db, x

STATUS_ORDER = ("new", "screening", "interview", "offer", "hired", "rejected", "reserve")


# ---------------------------------------------------------------- statuses

def test_statuses_and_keyboard():
    assert STATUSES == STATUS_ORDER and set(STATUS_LABELS) == set(STATUSES)
    kb = status_keyboard(7, "offer")
    cb = [b.callback_data for row in kb.inline_keyboard for b in row]
    assert cb == [f"ls:7:{s}" for s in ("new", "screening", "interview", "offer", "hired", "rejected", "reserve")]
    marked = [b.text for row in kb.inline_keyboard for b in row if b.text.startswith("•")]
    assert marked == ["• " + STATUS_LABELS["offer"]]


async def lead(sm, name: str = "Петров Борис"):
    async with sm() as s:
        return (await s.execute(text("SELECT id, status, version, stage_changed_at, updated_at FROM leads WHERE full_name = :n"),
                                {"n": name})).one()


async def events(sm, lead_id: int):
    async with sm() as s:
        return (await s.execute(text("SELECT kind, payload FROM lead_events WHERE lead_id = :i ORDER BY id"), {"i": lead_id})).all()


def test_status_buttons_cover_the_pipeline_and_bump_the_version():
    async def go(sm):
        before = await lead(sm, "Иванова Анна")  # new, version 1
        assert (before.status, before.version) == ("new", 1)
        for i, st in enumerate(("screening", "interview", "offer", "hired", "reserve", "rejected", "new"), start=2):
            cb = fake_callback(ADMIN, f"ls:{before.id}:{st}")
            await handlers.on_status_button(cb)
            assert STATUS_LABELS[st] in cb.answer.call_args.args[0]
            cur = await lead(sm, "Иванова Анна")
            assert (cur.status, cur.version) == (st, i)
        assert cur.stage_changed_at > before.stage_changed_at and cur.updated_at > before.updated_at
        ev = await events(sm, before.id)
        assert [e.payload["to"] for e in ev] == ["screening", "interview", "offer", "hired", "reserve", "rejected", "new"]
        assert all(e.kind == "status" and e.payload["via"] == "bot" for e in ev)
        # pressing the current status again is a no-op: no version bump, no event
        await handlers.on_status_button(fake_callback(ADMIN, f"ls:{before.id}:new"))
        assert (await lead(sm, "Иванова Анна")).version == 8 and len(await events(sm, before.id)) == 7

    run_db(go)


def test_old_contacted_button_means_screening():
    async def go(sm):
        cur = await lead(sm, "Иванова Анна")
        cb = fake_callback(ADMIN, f"ls:{cur.id}:contacted")  # a card sent before C4 still carries this button
        await handlers.on_status_button(cb)
        assert STATUS_LABELS["screening"] in cb.answer.call_args.args[0]
        assert (await lead(sm, "Иванова Анна")).status == "screening"
        async with sm() as s:
            assert (await queries.set_lead_status(s, cur.id, "contacted", "x"))[0] == "unchanged"
        assert (await events(sm, cur.id))[0].payload["to"] == "screening"

    run_db(go)


def test_revoked_lead_keeps_status_and_version():
    async def go(sm):
        rev = await lead(sm, "Сидорова Вера")  # consent revoked
        cb = fake_callback(ADMIN, f"ls:{rev.id}:offer")
        await handlers.on_status_button(cb)
        cur = await lead(sm, "Сидорова Вера")
        assert (cur.status, cur.version) == (rev.status, rev.version) and "отозвал" in cb.answer.call_args.args[0]

    run_db(go)


# ---------------------------------------------------------------- hr_message delivery

def test_default_texts():
    assert outbox.default_message("custom", "Анна", "operator") is None
    t = outbox.default_message("interview", "Анна", "operator")
    assert t.startswith("Здравствуйте, Анна!") and "интервью" in t and "«Оператор линии розлива»" in t
    t = outbox.default_message("offer", "Анна", "lab")
    assert "готовы предложить" in t and "«Лаборант ОТК»" in t
    t = outbox.default_message("reject", "Анна", None)
    assert "не готовы предложить" in t and "позицию" not in t
    assert outbox.default_message("offer", None, "pilot").startswith("Здравствуйте! ")  # no name, unknown role: still a sentence
    assert outbox.message_text({"template": "offer", "text": "  Свой текст  "}) == "Свой текст"
    assert outbox.message_text({"template": "offer", "text": "  ", "first_name": "Анна"}).startswith("Здравствуйте, Анна!")
    assert outbox.message_text({"template": "custom", "text": None}) is None


async def start_bot(sm, name: str) -> tuple[int, int, int]:
    """Mark the candidate's player as having started the bot; (lead id, user id, tg id)."""
    async with sm() as s, s.begin():
        row = (await s.execute(text("SELECT l.id, u.id AS uid, u.tg_id FROM leads l JOIN users u ON u.id = l.user_id WHERE l.full_name = :n"),
                               {"n": name})).one()
        await s.execute(text("UPDATE users SET bot_started_at = now() WHERE id = :u"), {"u": row.uid})
    return row.id, row.uid, row.tg_id


async def put_message(sm, lid: int, uid: int, tg: int, template: str = "custom", body: str | None = "Привет, <b>Анна</b>!", **extra) -> int:
    payload = {"lead_id": lid, "user_id": uid, "tg_id": tg, "template": template, "text": body, "first_name": "Анна",
               "vacancy": "operator", "hr_user_id": 1, **extra}
    async with sm() as s, s.begin():
        return await x(s, "INSERT INTO outbox (kind, payload) VALUES ('hr_message', CAST(:p AS jsonb)) RETURNING id", p=json.dumps(payload))


async def outbox_row(sm, oid: int):
    async with sm() as s:
        return (await s.execute(text("SELECT sent_at, attempts, last_error FROM outbox WHERE id = :i"), {"i": oid})).one()


def test_message_is_delivered_to_the_candidate_chat_as_plain_text():
    bot = make_bot()

    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        oid = await put_message(sm, lid, uid, tg)
        res = await outbox.deliver_messages(runtime.get())
        assert (res.sent, res.failed, res.skipped) == (1, 0, None)
        bot.send_message.assert_awaited_once()
        call = bot.send_message.call_args
        assert call.args == (tg, "Привет, <b>Анна</b>!") and call.kwargs == {"parse_mode": None}  # HR text is never parsed as HTML
        r = await outbox_row(sm, oid)
        assert r.sent_at is not None and r.attempts == 0 and r.last_error is None
        bot.send_message.reset_mock()
        assert (await outbox.deliver_messages(runtime.get())).sent == 0  # nothing left
        bot.send_message.assert_not_called()

    run_db(go, bot=bot)


def test_template_without_text_uses_the_default_text():
    bot = make_bot()

    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        await put_message(sm, lid, uid, tg, template="interview", body=None)
        await put_message(sm, lid, uid, tg, template="reject", body=None, vacancy=None)
        assert (await outbox.deliver_messages(runtime.get())).sent == 2
        first, second = [c.args[1] for c in bot.send_message.call_args_list]
        assert first.startswith("Здравствуйте, Анна!") and "интервью" in first and "Оператор линии розлива" in first
        assert "не готовы предложить" in second

    run_db(go, bot=bot)


def test_message_is_independent_of_the_hr_cards():
    bot = make_bot()

    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        await put_message(sm, lid, uid, tg, body="Только вам")
        async with sm() as s, s.begin():
            card = await x(s, "INSERT INTO outbox (kind, payload) VALUES ('lead_created', CAST(:p AS jsonb)) RETURNING id", p=json.dumps({"lead_id": lid}))
        res = await outbox.deliver_once(runtime.get())  # the HR-card job never touches hr_message rows
        assert res.sent == 1 and sorted(c.args[0] for c in bot.send_message.call_args_list) == [ADMIN, ADMIN2]
        assert (await outbox_row(sm, card)).sent_at is not None
        bot.send_message.reset_mock()
        res = await outbox.outbox_delivery()  # the scheduled job delivers the message
        assert res.sent == 1 and [c.args[0] for c in bot.send_message.call_args_list] == [tg]

    run_db(go, bot=bot)


def test_message_needs_no_hr_recipients():
    bot = make_bot()

    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        await put_message(sm, lid, uid, tg)
        res = await outbox.outbox_delivery()
        assert res.sent == 1 and res.skipped == "no_recipients"  # no HR chats at all, the candidate still gets the message
        assert [c.args[0] for c in bot.send_message.call_args_list] == [tg]

    run_db(go, bot=bot, settings=make_settings(hr_admin_tg_ids=frozenset()))


def test_rows_that_must_not_be_sent_are_closed_with_a_reason():
    bot = make_bot()

    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        rid, ruid, rtg = await start_bot(sm, "Сидорова Вера")  # consent revoked after HR queued the message
        oid_revoked = await put_message(sm, rid, ruid, rtg)
        async with sm() as s:  # Петров Борис never pressed /start
            pid, puid, ptg = (await s.execute(text("SELECT l.id, u.id, u.tg_id FROM leads l JOIN users u ON u.id = l.user_id "
                                                   "WHERE l.full_name = 'Петров Борис'"))).one()
        oid_not_started = await put_message(sm, pid, puid, ptg)
        oid_gone = await put_message(sm, lid, 9_999_999, 1)  # no such player
        oid_malformed = await put_message(sm, lid, uid, tg, template="custom", body=None)  # custom without any text
        async with sm() as s, s.begin():
            await s.execute(text("UPDATE users SET is_test = true WHERE id = :u"), {"u": uid})
        oid_test = await put_message(sm, lid, uid, tg)
        res = await outbox.deliver_messages(runtime.get())
        assert (res.sent, res.failed, res.test_skipped) == (0, 0, 5)
        bot.send_message.assert_not_called()
        for oid, reason in ((oid_revoked, "consent revoked"), (oid_not_started, "bot not started"), (oid_gone, "no such user"),
                            (oid_malformed, "malformed payload"), (oid_test, "bot or test user")):
            r = await outbox_row(sm, oid)
            assert r.sent_at is not None and r.last_error == f"not delivered: {reason}", (oid, r.last_error)

    run_db(go, bot=bot)


def test_blocked_bot_is_final_and_a_failure_is_retried_with_backoff():
    blocked = make_bot(fail=TelegramForbiddenError(SendMessage(chat_id=1, text="x"), "Forbidden: bot was blocked by the user"))

    async def go_blocked(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        oid = await put_message(sm, lid, uid, tg)
        res = await outbox.deliver_messages(runtime.get())
        assert (res.sent, res.failed) == (0, 0)
        r = await outbox_row(sm, oid)
        assert r.sent_at is not None and r.last_error == "not delivered: blocked the bot" and r.attempts == 0
        blocked.send_message.reset_mock()
        await outbox.deliver_messages(runtime.get())
        blocked.send_message.assert_not_called()  # not asked again

    run_db(go_blocked, bot=blocked)

    down = make_bot(fail=TelegramNetworkError(SendMessage(chat_id=1, text="x"), "boom"))

    async def go_down(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        oid = await put_message(sm, lid, uid, tg)
        res = await outbox.deliver_messages(runtime.get())
        assert (res.sent, res.failed) == (0, 1)
        r = await outbox_row(sm, oid)
        assert r.sent_at is None and r.attempts == 1 and "TelegramNetworkError" in r.last_error
        down.send_message.reset_mock()
        await outbox.deliver_messages(runtime.get())  # backoff: not retried at once
        down.send_message.assert_not_called()
        outbox._next_try.clear()
        down.send_message.side_effect = None
        res = await outbox.deliver_messages(runtime.get())  # Telegram is back
        assert res.sent == 1 and (await outbox_row(sm, oid)).sent_at is not None

    run_db(go_down, bot=down)


def test_without_a_bot_or_database_the_rows_wait():
    async def go(sm):
        lid, uid, tg = await start_bot(sm, "Иванова Анна")
        oid = await put_message(sm, lid, uid, tg)
        res = await outbox.deliver_messages(runtime.get())
        assert res.skipped == "no_bot"
        assert (await outbox_row(sm, oid)).sent_at is None

    run_db(go, bot=None)
