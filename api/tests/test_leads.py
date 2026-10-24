"""S5: POST /api/leads, GET /api/leads/me, DELETE /api/leads/me (sim is DOWN in every test: leads never call it)."""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, timedelta

import pytest

from app import leads as leads_mod
from app.leads import (LeadIn, age_on, classify_contact, clean, compute_tags, is_profile_direction, normalize_phone,
                       normalize_telegram, parse_course)
from tests.engage_helpers import (correct_answers, engage, engage_factory, js, lead_body,  # noqa: F401
                                  legacy_body, outbox, post_lead, seed_factory, user_id, wrong_answers, years_ago)
from tests.helpers import dev
from tests.test_errors import assert_error


# ---------------------------------------------------------------- pure helpers

@pytest.mark.parametrize("raw,expected", [
    ("@ivan_petrov", ("telegram", "ivan_petrov")),
    ("https://t.me/ivan_petrov", ("telegram", "ivan_petrov")),
    ("ivan_petrov", ("telegram", "ivan_petrov")),
    ("Ivan@Example.COM", ("email", "ivan@example.com")),
    ("8 (916) 123-45-67", ("phone", "+79161234567")),
    ("+7 916 123 45 67", ("phone", "+79161234567")),
    ("9161234567", ("phone", "+79161234567")),
    ("+380 50 123 45 67", ("phone", "+380501234567")),
    ("@iv", None), ("12345", None), ("hello world", None), ("a@b", None),
])
def test_classify_contact(raw, expected):
    assert classify_contact(raw) == expected


def test_normalizers_reject_garbage():
    assert normalize_phone("123") is None and normalize_phone("+1" + "2" * 20) is None
    assert normalize_telegram("@a b") is None and normalize_telegram("@1abcde") is None


@pytest.mark.parametrize("raw,expected", [
    ("4", ("4", 4, False)), (4, ("4", 4, False)), ("Выпускник", ("Выпускник", None, True)),
    ("3 курс", ("3 курс", 3, False)), ("магистратура", ("магистратура", None, False)), (None, (None, None, False)),
    ("", (None, None, False)),
])
def test_parse_course(raw, expected):
    assert parse_course(raw) == expected


def test_age_on_is_birthday_exact():
    today = date(2026, 10, 24)
    assert age_on(date(2008, 10, 24), today) == 18  # birthday today
    assert age_on(date(2008, 10, 25), today) == 17  # tomorrow
    assert age_on(date(2008, 2, 29), date(2026, 2, 28)) == 17


@pytest.mark.parametrize("direction,expected", [
    ("Химическая технология", True), ("Технология продуктов питания", True), ("Автоматизация техпроцессов", True),
    ("Логистика", True), ("Управление качеством", True), ("Механика и робототехника", True), ("chemistry", True),
    ("Менеджмент", False), ("Юриспруденция", False), ("", False), (None, False)])
def test_profile_directions(direction, expected):
    assert is_profile_direction(direction) is expected


def test_compute_tags_unit():
    on = date(2026, 10, 24)
    body = lambda **o: clean(LeadIn(**{**lead_body(), **o}), on)  # noqa: E731
    assert compute_tags(body(course="3", direction="Юриспруденция", readiness="later"), 0, on) == []
    assert compute_tags(body(course="4", direction="Юриспруденция", readiness="later"), 0, on) == ["senior"]
    assert compute_tags(body(course="Выпускник", direction="Юриспруденция", readiness="later"), 0, on) == ["senior"]
    assert compute_tags(body(course="1", direction="Химия", readiness="later"), 0, on) == ["profile_direction"]
    assert compute_tags(body(course="1", direction="Менеджмент", readiness="now"), 0, on) == ["ready_for_season"]
    soon = (on + timedelta(days=100)).isoformat()
    far = (on + timedelta(days=200)).isoformat()
    assert "ready_for_season" in compute_tags(body(course="1", direction="x", readiness=None, readyFrom=soon), 0, on)
    assert "ready_for_season" not in compute_tags(body(course="1", direction="x", readiness=None, readyFrom=far), 0, on)
    assert compute_tags(body(course="1", direction="x", readiness="later"), 6, on) == ["high_quiz"]
    assert compute_tags(body(course="1", direction="x", readiness="later"), 5, on) == []
    assert compute_tags(body(), 10, on) == ["senior", "profile_direction", "ready_for_season", "high_quiz"]


# ---------------------------------------------------------------- POST /api/leads: happy paths

async def test_create_lead_full_form(engage):
    r = await post_lead(engage)
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["ok"] is True and lead["status"] == "new" and lead["id"] > 0
    assert lead["fullName"] == "Иванов Иван Иванович" and lead["phone"] == "+79161234567"
    assert lead["email"] == "ivan@example.com" and lead["telegram"] == "ivan_petrov"
    assert lead["university"] == "РХТУ им. Менделеева" and lead["course"] == "4" and lead["vacancy"] == "lab"
    assert lead["practiceType"] == "paid" and lead["readiness"] == "season" and lead["consent"] is True
    assert lead["consentTextVersion"] == "2026-10-v1" and lead["consentRevokedAt"] is None
    assert set(lead["tags"]) == {"senior", "profile_direction", "ready_for_season"}
    assert lead["bestProfession"] is None and lead["seasonPlace"] is None and lead["quizScore"] is None
    assert "hrNote" not in lead and "hr_note" not in lead
    row = await engage.db.fetchrow("SELECT * FROM leads")
    assert row["user_id"] == await user_id(engage) and row["status"] == "new" and row["consent"] is True
    assert row["birth_date"] == date.fromisoformat(years_ago(21)) and row["consent_at"] is not None
    assert row["tags"] == lead["tags"] and row["season_id"] is not None
    assert await engage.db.fetchval("SELECT count(*) FROM seasons") == 1  # created for the lead (nobody bootstrapped)


async def test_create_lead_snake_case_fields_work_too(engage):
    r = await engage.post("/api/leads", headers=dev(1), json={
        "full_name": "Анна Петрова", "birth_date": years_ago(30), "email": "anna@example.com", "university": "МИФИ",
        "practice_type": "free", "consent": True, "consent_text_version": "v1", "ready_from": years_ago(0)})
    assert r.status_code == 201, r.text
    assert r.json()["practiceType"] == "free" and "ready_for_season" in r.json()["tags"]


async def test_create_lead_legacy_frontend_form(engage):
    """The vendored form (no birth date, no case fields): needs the age confirmation; `contact` is mapped by format."""
    await engage.get("/api/me", headers=dev(1))
    await engage.db.execute("UPDATE users SET age_confirmed = true")
    r = await engage.post("/api/leads", json=legacy_body(), headers=dev(1))
    assert r.status_code == 201, r.text
    lead = r.json()
    assert lead["telegram"] == "petr_sidorov" and lead["phone"] is None and lead["email"] is None
    assert lead["vacancy"] == "operator" and lead["birthDate"] is None and lead["tags"] == []
    assert_error(await engage.post("/api/leads", json=legacy_body(), headers=dev(2)), 403, "age_required")  # tg 2: unconfirmed
    await engage.db.execute("UPDATE users SET age_confirmed = true WHERE tg_id = 2")
    for contact, col, value in (("petr@mail.ru", "email", "petr@mail.ru"), ("+7 (999) 000-11-22", "phone", "+79990001122")):
        r = await engage.post("/api/leads", json=legacy_body(contact=contact), headers=dev(2))
        assert r.status_code == 201 and r.json()[col] == value
        await engage.db.execute("DELETE FROM leads WHERE user_id = $1", await user_id(engage, 2))


async def test_explicit_contact_fields_win_over_legacy_contact(engage):
    r = await post_lead(engage, phone=None, email=None, telegram="@real_one", contact="@other_one")
    assert r.status_code == 201 and r.json()["telegram"] == "real_one"


async def test_age_confirmed_in_factory_snapshot_counts(engage):
    """ageConfirm lives in the sim state / stored snapshot (player.ageConfirmed): enough when no birth date is sent."""
    await seed_factory(engage, 1, snapshot={"player": {"ageConfirmed": True}})
    assert (await post_lead(engage, birthDate=None)).status_code == 201


async def test_enrichment_from_factory_and_quiz(engage):
    snap = {"skills": {"byProfession": {"lab": {"played": 5, "wins": 5, "avgScore": 0.9},
                                        "operator": {"played": 5, "wins": 1, "avgScore": 0.3}}}}
    await seed_factory(engage, 1, stars=40, snapshot=snap)
    await seed_factory(engage, 2, stars=90)
    uid = await user_id(engage, 1)
    for a in correct_answers(8, index=True):
        await engage.db.execute("INSERT INTO quiz_answers (user_id, question_id, correct, answer) VALUES ($1, $2, true, $3)",
                                uid, a["question_id"], a["answer"])
    lead = (await post_lead(engage)).json()
    assert lead["bestProfession"] == "lab" and lead["seasonPlace"] == 2 and lead["quizScore"] == 8
    assert "high_quiz" in lead["tags"]


async def test_outbox_lead_created_in_same_transaction(engage):
    await engage.get("/api/me", headers=dev(7))
    await engage.db.execute("UPDATE users SET source = 'qr_fair' WHERE tg_id = 7")
    lead = (await post_lead(engage, 7)).json()
    rows = await outbox(engage)
    assert [r["kind"] for r in rows] == ["lead_created"]
    row = rows[0]
    assert row["sent_at"] is None and row["attempts"] == 0 and row["last_error"] is None
    p = row["payload"]
    assert p["lead_id"] == lead["id"] and p["tg_id"] == 7 and p["source"] == "qr_fair" and p["full_name"] == lead["fullName"]
    assert p["tags"] == lead["tags"] and p["university"] == lead["university"] and p["telegram"] == "ivan_petrov"


# ---------------------------------------------------------------- GET / DELETE /api/leads/me

async def test_get_my_lead(engage):
    assert_error(await engage.get("/api/leads/me", headers=dev(1)), 404, "lead_not_found")
    created = (await post_lead(engage)).json()
    r = await engage.get("/api/leads/me", headers=dev(1))
    assert r.status_code == 200 and r.json() == {k: v for k, v in created.items() if k != "ok"}
    assert_error(await engage.get("/api/leads/me", headers=dev(2)), 404, "lead_not_found")  # per user


async def test_revoke_is_idempotent_and_keeps_status(engage):
    assert_error(await engage.delete("/api/leads/me", headers=dev(1)), 404, "lead_not_found")
    await post_lead(engage)
    await engage.db.execute("UPDATE leads SET status = 'interview'")
    r = await engage.delete("/api/leads/me", headers=dev(1))
    assert r.status_code == 204 and r.content == b""
    row = await engage.db.fetchrow("SELECT * FROM leads")
    assert row["consent_revoked_at"] is not None and row["status"] == "interview" and row["consent"] is True
    first = row["consent_revoked_at"]
    assert [o["kind"] for o in await outbox(engage)] == ["lead_created", "lead_revoked"]
    # again: 204, nothing changes, no second outbox row
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert await engage.db.fetchval("SELECT consent_revoked_at FROM leads") == first
    revoked = await outbox(engage, "lead_revoked")
    assert len(revoked) == 1 and revoked[0]["payload"]["lead_id"] == row["id"]
    # the owner no longer sees it (forgotten), the row stays in the DB for the audit
    assert_error(await engage.get("/api/leads/me", headers=dev(1)), 404, "lead_not_found")
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 1


async def test_revoked_player_may_submit_again_in_the_same_season(engage):
    first = (await post_lead(engage)).json()
    await engage.db.execute("UPDATE leads SET status = 'interview'")
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert_error(await engage.get("/api/leads/me", headers=dev(1)), 404, "lead_not_found")
    again = await post_lead(engage, fullName="Новое Имя")
    assert again.status_code == 201 and again.json()["id"] != first["id"] and again.json()["status"] == "new"
    # the new form is what the owner sees; the revoked row is kept
    me = (await engage.get("/api/leads/me", headers=dev(1))).json()
    assert me["id"] == again.json()["id"] and me["fullName"] == "Новое Имя" and me["consentRevokedAt"] is None
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 2
    assert await engage.db.fetchval("SELECT consent_revoked_at IS NOT NULL FROM leads WHERE id = $1", first["id"])
    # one ACTIVE lead per season still holds
    assert_error(await post_lead(engage), 409, "lead_already_exists")
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 2
    # the next revoke touches only the active one (one more outbox row), the old row keeps its timestamp
    old_ts = await engage.db.fetchval("SELECT consent_revoked_at FROM leads WHERE id = $1", first["id"])
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert [o["payload"]["lead_id"] for o in await outbox(engage, "lead_revoked")] == [first["id"], again.json()["id"]]
    assert await engage.db.fetchval("SELECT consent_revoked_at FROM leads WHERE id = $1", first["id"]) == old_ts
    assert_error(await engage.get("/api/leads/me", headers=dev(1)), 404, "lead_not_found")


async def test_revoke_hits_every_lead_of_the_player(engage):
    await post_lead(engage)
    await engage.db.execute("UPDATE seasons SET closed = true")
    await engage.db.execute("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7)")
    assert (await post_lead(engage)).status_code == 201
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert await engage.db.fetchval("SELECT count(*) FROM leads WHERE consent_revoked_at IS NOT NULL") == 2
    assert len(await outbox(engage, "lead_revoked")) == 2


async def test_revoke_is_per_user(engage):
    await post_lead(engage, 1)
    await post_lead(engage, 2)
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204
    assert await engage.db.fetchval(
        "SELECT consent_revoked_at FROM leads WHERE user_id = $1", await user_id(engage, 2)) is None


# ---------------------------------------------------------------- errors

async def test_401_everywhere(engage):
    for method, path in (("POST", "/api/leads"), ("GET", "/api/leads/me"), ("DELETE", "/api/leads/me")):
        r = await engage.request(method, path, json=lead_body() if method == "POST" else None)
        assert_error(r, 401, "unauthorized")


async def test_consent_required(engage):
    for over in ({"consent": False}, {"consent": None}, {"consentTextVersion": None}, {"consentTextVersion": "   "}):
        err = assert_error(await post_lead(engage, **over), 403, "consent_required")
        assert err["details"]["requires"]
    r = await post_lead(engage, consent="true")  # a string is not consent
    assert r.status_code == 422
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0
    assert await outbox(engage) == []


async def test_age_required_under_18(engage):
    err = assert_error(await post_lead(engage, birthDate=years_ago(17, 30)), 403, "age_required")
    assert err["details"] == {"min_age": 18}
    assert_error(await post_lead(engage, birthDate=years_ago(18, -1)), 403, "age_required")  # turns 18 tomorrow
    # a confirmed age does not outvote an explicit under-age birth date
    await engage.db.execute("UPDATE users SET age_confirmed = true")
    assert_error(await post_lead(engage, birthDate=years_ago(15)), 403, "age_required")
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0 and await outbox(engage) == []


async def test_age_required_when_missing_and_not_confirmed(engage):
    assert_error(await post_lead(engage, birthDate=None), 403, "age_required")
    await engage.db.execute("UPDATE users SET age_confirmed = true")
    assert (await post_lead(engage, birthDate=None)).status_code == 201  # confirmed: no date needed


async def test_exactly_18_today_is_allowed(engage):
    r = await post_lead(engage, birthDate=years_ago(18))
    assert r.status_code == 201, r.text


@pytest.mark.parametrize("over,field", [
    ({"fullName": None}, "fullName"), ({"fullName": "A"}, "fullName"), ({"fullName": "x" * 121}, "fullName"),
    ({"university": None}, "university"), ({"university": "  "}, "university"),
    ({"phone": None, "email": None, "telegram": None}, "contact"),
    ({"phone": "12"}, "phone"), ({"email": "nope"}, "email"), ({"telegram": "@x"}, "telegram"),
    ({"phone": None, "email": None, "telegram": None, "contact": "just words"}, "contact"),
    ({"birthDate": "21.05.2000"}, "birthDate"), ({"birthDate": "2999-01-01"}, "birthDate"),
    ({"birthDate": "1800-01-01"}, "birthDate"), ({"readyFrom": "soon"}, "readyFrom"),
    ({"vacancy": "ceo"}, "vacancy"), ({"practiceType": "volunteer"}, "practiceType"),
    ({"readiness": "maybe"}, "readiness"), ({"course": "15"}, "course"), ({"course": 0}, "course"),
    ({"fullName": "Иван\u0000Иванов"}, "fullName"),
])
async def test_validation_error(engage, over, field):
    err = assert_error(await post_lead(engage, **over), 422, "validation_error")
    assert any(field in e["loc"] for e in err["details"]["errors"]), err
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0


async def test_validation_error_on_malformed_json(engage):
    r = await engage.post("/api/leads", content=b"{not json", headers={**dev(1), "content-type": "application/json"})
    assert_error(r, 422, "validation_error")


async def test_lead_already_exists_409(engage):
    assert (await post_lead(engage)).status_code == 201
    err = assert_error(await post_lead(engage, fullName="Другое Имя"), 409, "lead_already_exists")
    assert err["details"]["season_id"] and "revoked" not in err["details"]
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 1
    assert len(await outbox(engage)) == 1  # the rejected duplicate wrote no outbox row
    assert await engage.db.fetchval("SELECT full_name FROM leads") == "Иванов Иван Иванович"


async def test_double_submit_race_one_wins(engage):
    rs = await asyncio.gather(*(post_lead(engage) for _ in range(4)))
    codes = sorted(r.status_code for r in rs)
    assert codes == [201, 409, 409, 409], codes
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 1 and len(await outbox(engage)) == 1


async def test_one_lead_per_season_not_per_lifetime(engage):
    assert (await post_lead(engage)).status_code == 201
    await engage.db.execute("UPDATE seasons SET closed = true")
    await engage.db.execute("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7)")
    r = await post_lead(engage)
    assert r.status_code == 201
    assert await engage.db.fetchval("SELECT count(DISTINCT season_id) FROM leads") == 2
    assert (await engage.get("/api/leads/me", headers=dev(1))).json()["id"] == r.json()["id"]  # latest


# ---------------------------------------------------------------- atomicity: lead + outbox in ONE transaction

async def test_lead_rolled_back_when_outbox_insert_fails(engage, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("outbox is broken")

    monkeypatch.setattr(leads_mod, "enqueue", boom)
    assert_error(await post_lead(engage), 500, "internal_error")
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0
    assert await engage.db.fetchval("SELECT count(*) FROM outbox") == 0
    monkeypatch.undo()
    assert (await post_lead(engage)).status_code == 201  # the player can simply retry
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 1


async def test_outbox_row_lost_with_the_lead_on_failure_after_enqueue(engage, monkeypatch):
    """Failure AFTER the outbox row was written (e.g. the log call): neither survives."""
    real = leads_mod.enqueue

    async def enqueue_then_fail(session, kind, payload):
        await real(session, kind, payload)
        raise RuntimeError("late failure")

    monkeypatch.setattr(leads_mod, "enqueue", enqueue_then_fail)
    assert_error(await post_lead(engage), 500, "internal_error")
    assert await engage.db.fetchval("SELECT count(*) FROM leads") == 0
    assert await engage.db.fetchval("SELECT count(*) FROM outbox") == 0


async def test_revoke_rolled_back_when_outbox_insert_fails(engage, monkeypatch):
    await post_lead(engage)

    async def boom(*a, **k):
        raise RuntimeError("outbox is broken")

    monkeypatch.setattr(leads_mod, "enqueue", boom)
    assert_error(await engage.delete("/api/leads/me", headers=dev(1)), 500, "internal_error")
    assert await engage.db.fetchval("SELECT consent_revoked_at FROM leads") is None
    monkeypatch.undo()
    assert (await engage.delete("/api/leads/me", headers=dev(1))).status_code == 204


async def test_500_is_the_unified_error(engage, monkeypatch):
    async def boom(self, user):
        raise RuntimeError("secret internal detail")

    monkeypatch.setattr(leads_mod.LeadService, "current", boom)
    err = assert_error(await engage.get("/api/leads/me", headers=dev(1)), 500, "internal_error")
    assert "secret" not in json.dumps(err)


# ---------------------------------------------------------------- 429

async def test_429_on_the_sixth_post_in_a_minute(engage_factory):
    c = await engage_factory(rate_limit_enabled=True)  # real limits: leads = 5 / min
    for i in range(5):  # invalid bodies still count; 422 until the limit bites
        assert (await post_lead(c, fullName=None)).status_code == 422, i
    r = await post_lead(c, fullName=None)
    err = assert_error(r, 429, "rate_limited")
    assert int(r.headers["retry-after"]) >= 1 and err["details"]["limit"] == 5
    assert (await post_lead(c, 2, fullName=None)).status_code == 422  # another user is unaffected


# ---------------------------------------------------------------- personal data stays out of the logs

PII = ("Иванов Иван Иванович", "+79161234567", "ivan@example.com", "ivan_petrov", "Секретная Фамилия", "secret.person@example.org")


async def test_personal_data_not_in_logs(engage, caplog):
    caplog.set_level(logging.DEBUG)
    await post_lead(engage)  # 201
    await post_lead(engage)  # 409
    await post_lead(engage, 2, email="secret.person@example.org", fullName="Секретная Фамилия", phone="1")  # 422
    await post_lead(engage, 3, email="secret.person@example.org", fullName="Секретная Фамилия", birthDate=years_ago(10))  # 403
    await engage.get("/api/leads/me", headers=dev(1))
    await engage.delete("/api/leads/me", headers=dev(1))
    text = "\n".join(f"{r.getMessage()} {getattr(r, 'fields', '')} {r.exc_text or ''}" for r in caplog.records)
    assert "request" in text and "lead created" in text  # something was logged
    for secret in PII:
        assert secret not in text, secret


async def test_engine_hides_sql_parameters(engage):
    assert engage.app.state.engine.sync_engine.hide_parameters is True


# ---------------------------------------------------------------- season summary integration

async def test_summary_shows_quiz_result_and_lead_status(game):
    """`game` = fake sim that works: the factory exists. quizResult / leadStatus come from the S5 tables."""
    from tests.game_helpers import bootstrap

    await bootstrap(game, 1)
    s = (await game.get("/api/season/summary", headers=dev(1))).json()
    assert s["quizResult"] is None and s["leadStatus"] is None
    r = await game.post("/api/quiz/answer", json=correct_answers(4) + [], headers=dev(1))
    assert r.status_code == 200
    s = (await game.get("/api/season/summary", headers=dev(1))).json()
    assert s["quizResult"] == {"correct": 4, "total": 4, "score": 100, "answered": 4, "completed": False, "bankTotal": 8}
    r = await post_lead(game)
    assert r.status_code == 201 and r.json()["quizScore"] == 4
    assert (await game.get("/api/season/summary", headers=dev(1))).json()["leadStatus"] == "new"
    await game.delete("/api/leads/me", headers=dev(1))
    assert (await game.get("/api/season/summary", headers=dev(1))).json()["leadStatus"] == "revoked"  # only revoked ones exist
    assert (await post_lead(game)).status_code == 201  # a new form after the revocation: the latest non-revoked wins
    assert (await game.get("/api/season/summary", headers=dev(1))).json()["leadStatus"] == "new"
    await game.db.execute("UPDATE leads SET status = 'interview' WHERE consent_revoked_at IS NULL")
    assert (await game.get("/api/season/summary", headers=dev(1))).json()["leadStatus"] == "interview"
