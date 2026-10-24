"""S6 outbox delivery to HR: marks sent, retries with backoff on failure, never crashes, never consumes without a bot."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone

from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import text

from app.hr import outbox, runtime
from hr_support import ADMIN, ADMIN2, add_lead, add_user, make_bot, make_settings, run_db, x


async def lead_id(sm, name: str) -> int:
    async with sm() as s:
        return await x(s, "SELECT id FROM leads WHERE full_name = :n", n=name)


async def put(sm, kind: str, payload: dict, **cols) -> int:
    async with sm() as s, s.begin():
        oid = await x(s, "INSERT INTO outbox (kind, payload) VALUES (:k, CAST(:p AS jsonb)) RETURNING id", k=kind, p=json.dumps(payload))
        for col, val in cols.items():
            await x(s, f"UPDATE outbox SET {col} = :v WHERE id = :i", v=val, i=oid)
    return oid


async def row(sm, oid: int):
    async with sm() as s:
        return (await s.execute(text("SELECT kind, sent_at, attempts, last_error FROM outbox WHERE id = :i"), {"i": oid})).one()


def calls(bot):
    return [(c.args[0], c.args[1], c.kwargs.get("reply_markup")) for c in bot.send_message.call_args_list]


def test_lead_created_is_delivered_to_every_admin_and_marked_sent():
    bot = make_bot()

    async def go(sm):
        lid = await lead_id(sm, "Иванова Анна")
        oid = await put(sm, "lead_created", {"lead_id": lid, "full_name": "stale name from payload"})
        res = await outbox.deliver_once(runtime.get())
        assert (res.sent, res.failed, res.skipped) == (1, 0, None)
        r = await row(sm, oid)
        assert r.sent_at is not None and r.attempts == 0 and r.last_error is None
        sent = calls(bot)
        assert sorted(c[0] for c in sent) == [ADMIN, ADMIN2]
        chat, body, markup = sent[0]
        assert "Иванова Анна" in body and "stale name" not in body  # rendered from the DB, not the payload
        assert "+79991112233" in body and "Оператор линии розлива" in body and "Новая анкета" in body
        buttons = [b for row_ in markup.inline_keyboard for b in row_]
        assert len(buttons) == 5 and all(b.callback_data.startswith(f"ls:{lid}:") for b in buttons)
        # nothing left: a second run sends nothing
        bot.send_message.reset_mock()
        assert (await outbox.deliver_once(runtime.get())).sent == 0
        bot.send_message.assert_not_called()

    run_db(go, bot=bot)


def test_hr_chat_id_overrides_the_admin_list():
    bot = make_bot()

    async def go(sm):
        oid = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Петров Борис")})
        await outbox.deliver_once(runtime.get())
        assert [c[0] for c in calls(bot)] == [-100777]
        assert (await row(sm, oid)).sent_at is not None

    run_db(go, bot=bot, settings=make_settings(hr_chat_id=-100777))


def test_payload_fallback_when_the_lead_row_is_gone():
    bot = make_bot()

    async def go(sm):
        await put(sm, "lead_created", {"lead_id": 424242, "full_name": "Из <payload>", "university": "ВУЗ", "tags": ["senior"],
                                       "quiz_score": 4, "source": "qr_1"})
        assert (await outbox.deliver_once(runtime.get())).sent == 1
        body = calls(bot)[0][1]
        assert "Из &lt;payload&gt;" in body and "#senior" in body and "квиз: 4/10" in body and "qr_1" in body

    run_db(go, bot=bot)


def test_revoked_lead_card_has_no_buttons_and_revocation_notice_is_short():
    bot = make_bot()

    async def go(sm):
        rid = await lead_id(sm, "Сидорова Вера")
        await put(sm, "lead_created", {"lead_id": rid})  # revoked after it was created: shown as revoked
        await put(sm, "lead_revoked", {"lead_id": rid, "revoked_at": "2026-01-01T00:00:00Z"})
        assert (await outbox.deliver_once(runtime.get())).sent == 2
        (_, card, card_kb), (_, notice, notice_kb) = calls(bot)
        assert card_kb is None and "согласие отозвано" in card and "+79993334455" not in card
        assert notice_kb is None and "Отзыв согласия" in notice and "Сидорова Вера" in notice and len(notice) < 400

    run_db(go, bot=bot, settings=make_settings(hr_admin_tg_ids=frozenset({ADMIN})))


def test_other_kinds_and_sent_rows_are_left_alone():
    bot = make_bot()

    async def go(sm):
        other = await put(sm, "order_reminder", {"x": 1})
        done = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Петров Борис")}, sent_at=datetime.now(timezone.utc))
        res = await outbox.deliver_once(runtime.get())
        assert (res.sent, res.failed) == (0, 0)
        bot.send_message.assert_not_called()
        assert (await row(sm, other)).sent_at is None and (await row(sm, other)).attempts == 0
        assert (await row(sm, done)).attempts == 0

    run_db(go, bot=bot)


def test_failure_counts_attempts_backs_off_and_recovers():
    bot = make_bot(fail=TelegramNetworkError(method=SendMessage(chat_id=1, text="x"), message="telegram is down"))

    async def go(sm):
        oid = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")})
        res = await outbox.deliver_once(runtime.get())  # must not raise
        assert (res.sent, res.failed) == (0, 1)
        r = await row(sm, oid)
        assert r.sent_at is None and r.attempts == 1 and "TelegramNetworkError" in r.last_error and "telegram is down" in r.last_error
        n = bot.send_message.call_count
        assert n == 2  # both admins were tried
        # backoff: the very next tick leaves the row alone
        await outbox.deliver_once(runtime.get())
        assert bot.send_message.call_count == n and (await row(sm, oid)).attempts == 1
        # ... until the delay has passed
        outbox._next_try[oid] = time.monotonic() - 1
        await outbox.deliver_once(runtime.get())
        assert (await row(sm, oid)).attempts == 2
        # Telegram is back
        runtime.get().bot = make_bot()
        outbox._next_try.clear()
        assert (await outbox.deliver_once(runtime.get())).sent == 1
        r = await row(sm, oid)
        assert r.sent_at is not None and r.attempts == 2

    run_db(go, bot=bot)


def test_failure_does_not_burn_the_other_rows_and_retry_after_is_honoured():
    err = TelegramRetryAfter(method=SendMessage(chat_id=1, text="x"), message="flood", retry_after=120)
    bot = make_bot(fail=err)

    async def go(sm):
        a = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")})
        b = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Петров Борис")})
        before = time.monotonic()
        res = await outbox.deliver_once(runtime.get())
        assert res.failed == 1  # the tick stops at the first failure
        assert (await row(sm, a)).attempts == 1 and (await row(sm, b)).attempts == 0
        assert outbox._next_try[a] >= before + 120
        # the next tick skips `a` (backoff) and tries `b`
        await outbox.deliver_once(runtime.get())
        assert (await row(sm, b)).attempts == 1 and (await row(sm, a)).attempts == 1

    run_db(go, bot=bot)


def test_partial_delivery_counts_as_sent_and_records_the_failed_chat():
    bot = make_bot()

    async def flaky(chat, *a, **k):
        if chat == ADMIN2:
            raise RuntimeError("Forbidden: bot was blocked by the user")

    bot.send_message.side_effect = flaky

    async def go(sm):
        oid = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")})
        assert (await outbox.deliver_once(runtime.get())).sent == 1
        r = await row(sm, oid)
        assert r.sent_at is not None and r.attempts == 0 and str(ADMIN2) in r.last_error and "blocked" in r.last_error

    run_db(go, bot=bot)


def test_without_a_bot_or_recipients_the_outbox_is_not_consumed():
    async def go(sm):
        oid = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")})
        res = await outbox.outbox_delivery()  # the scheduler entry point, runtime has bot=None
        assert res.skipped == "no_bot"
        runtime.get().bot = make_bot()
        runtime.get().settings = make_settings(hr_admin_tg_ids=frozenset())
        res = await outbox.outbox_delivery()
        assert res.skipped == "no_recipients"
        r = await row(sm, oid)
        assert r.sent_at is None and r.attempts == 0 and r.last_error is None

    run_db(go, bot=None)


def test_rows_over_the_attempt_cap_are_not_retried():
    bot = make_bot()

    async def go(sm):
        oid = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")}, attempts=outbox.MAX_ATTEMPTS)
        assert (await outbox.deliver_once(runtime.get())).sent == 0
        bot.send_message.assert_not_called()
        assert (await row(sm, oid)).sent_at is None

    run_db(go, bot=bot)


def test_locked_rows_are_skipped_not_waited_for():
    bot = make_bot()

    async def go(sm):
        a = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Иванова Анна")})
        b = await put(sm, "lead_created", {"lead_id": await lead_id(sm, "Петров Борис")})
        async with sm() as holder, holder.begin():  # another worker is delivering row `a`
            await holder.execute(text("SELECT id FROM outbox WHERE id = :i FOR UPDATE"), {"i": a})
            res = await outbox.deliver_once(runtime.get())
        assert res.sent == 1
        assert (await row(sm, a)).sent_at is None and (await row(sm, b)).sent_at is not None

    run_db(go, bot=bot, settings=make_settings(hr_admin_tg_ids=frozenset({ADMIN})))


def test_the_job_swallows_database_errors():
    bot = make_bot()

    async def go(sm):
        def boom():
            raise ConnectionRefusedError("db is restarting")

        runtime.get().sm = boom
        assert await outbox.outbox_delivery() is None  # logged, not raised
        runtime._rt = None
        assert await outbox.outbox_delivery() is None  # not configured yet

    run_db(go, bot=bot)


def test_new_lead_to_card_roundtrip_with_status_change():
    """A lead created after the outbox row (a second card) shows the current status: cards are rendered from the DB."""
    bot = make_bot()

    async def go(sm):
        async with sm() as s, s.begin():
            u = await add_user(s, 801, "Новый")
            lid = await add_lead(s, u, 2, "Свежий Кандидат", "МИФИ", status="interview")
        await put(sm, "lead_created", {"lead_id": lid})
        await outbox.deliver_once(runtime.get())
        body, markup = calls(bot)[0][1], calls(bot)[0][2]
        assert "Собеседование" in body
        assert any(b.text.startswith("• ") and "Собеседование" in b.text for r in markup.inline_keyboard for b in r)

    run_db(go, bot=bot, settings=make_settings(hr_admin_tg_ids=frozenset({ADMIN})))


def test_process_idles_and_stops_cleanly_without_token_even_with_an_unreachable_database():
    import asyncio

    from app.config import Settings
    from app.main import amain

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(amain(Settings(bot_token="", database_url="postgresql+asyncpg://x:y@127.0.0.1:1/none",
                                                  hr_admin_tg_ids=frozenset({ADMIN})), stop))
        await asyncio.sleep(0.3)
        assert not task.done()
        res = await outbox.outbox_delivery()  # what the scheduler would run: no bot -> skipped before any DB access
        assert res is not None and res.skipped == "no_bot"
        stop.set()
        assert await asyncio.wait_for(task, 5) == 0
        assert runtime.get() is None  # closed on shutdown

    asyncio.run(go())
