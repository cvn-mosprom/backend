"""C5: `GET /api/hr/analytics` and `GET /api/hr/export.csv`. Real Postgres + Redis (skipped when absent)."""
from __future__ import annotations

import csv
import io
import json
import re

import pytest
import pytest_asyncio

from app.hr import analytics as an
from app.hr import export as exp
from tests.helpers import DeadRedis, dev
from tests.test_c3_hr_access import STRANGER, as_, hr  # noqa: F401  (the `hr` fixture: one HR user per role)
from tests.test_errors import assert_error

ANALYTICS = "/api/hr/analytics"
EXPORT = "/api/hr/export.csv"
ROLES = ["analyst", "hr", "hr_lead", "admin"]
# landing leads (no player) would survive a TRUNCATE of users and break the downgrade test of test_db.py: leave nothing behind
CLEANUP = "TRUNCATE leads, seasons, users RESTART IDENTITY CASCADE"
FULL = '{"speed": 80, "accuracy": 60, "attention": 40, "endurance": 20, "reliability": 100, "company": 0}'
HALF = '{"speed": 60, "accuracy": 40, "attention": 40, "endurance": 0, "reliability": 50, "company": 0}'


# ---------------------------------------------------------------- seeding

async def season(db) -> int:
    return await db.fetchval("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now() - interval '90 days', now() + interval '7 days', 97) RETURNING id")


async def user(db, tg: int, *, source=None, days_ago=0, bot=False, test=False) -> int:
    return await db.fetchval(
        "INSERT INTO users (tg_id, first_name, source, is_bot, is_test, created_at) "
        "VALUES ($1, $2, $3, $4, $5, now() - make_interval(days => $6)) RETURNING id", tg, f"U{tg}", source, bot, test, days_ago)


async def lead(db, season_id: int, uid: int | None, *, name="Кандидат Тестов", uni="МГУ", status="new", source=None, days_ago=0,
               revoked=False, tags=(), vacancy=None, phone=None, assignee=None, points=None, comp=None, fit=None) -> int:
    lid = await db.fetchval(
        "INSERT INTO leads (user_id, season_id, full_name, university, status, source, consent, consent_at, consent_text_version, "
        "consent_revoked_at, tags, vacancy, phone, assignee_id, created_at) "
        "VALUES ($1, $2, $3, $4, $5, $6, true, now(), 'v1', CASE WHEN $7 THEN now() END, $8, $9, $10, $11, now() - make_interval(days => $12)) "
        "RETURNING id", uid, season_id, name, uni, status, source, revoked, list(tags), vacancy, phone, assignee, days_ago)
    if uid is not None:
        await db.execute("INSERT INTO factories (user_id, season_id, name, state) VALUES ($1, $2, 'F', '{}'::jsonb)", uid, season_id)
    if uid is not None and (points is not None or comp is not None or fit is not None):
        await db.execute("INSERT INTO player_scores (user_id, season_id, achievement_points, competency, fit) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)",
                         uid, season_id, points or 0, comp or "{}", fit or "{}")
    return lid


async def event(db, uid: int, name: str, days_ago=0) -> None:
    await db.execute("INSERT INTO events (user_id, name, ts) VALUES ($1, $2, now() - make_interval(days => $3))", uid, name, days_ago)


@pytest_asyncio.fixture
async def world(hr):
    """Four leads in the last week, one 20 and one 60 days old, plus everything that must never count."""
    db, s = hr.db, await season(hr.db)
    u1, u2, u3, u4 = (await user(db, 8801, source="tg_a"), await user(db, 8802, source="tg_a"), await user(db, 8803, source="qr_b"),
                      await user(db, 8804))
    uc = await user(db, 8805, source="tg_a")  # revoked
    ub, ut = await user(db, 8806, source="tg_a", bot=True), await user(db, 8807, source="tg_a", test=True)
    u5, u6 = await user(db, 8808, source="tg_a", days_ago=20), await user(db, 8809, source="old_src", days_ago=60)
    hr.leads = {
        "hired": await lead(db, s, u1, uni="МГУ", status="hired", points=750, comp=FULL),
        "interview": await lead(db, s, u2, uni="МГУ", status="interview", points=120, comp=HALF),
        "new": await lead(db, s, u3, uni="МГТУ", status="new"),  # no score row
        "landing": await lead(db, s, None, uni="МГУ", status="screening", source="landing-hr"),
        "revoked": await lead(db, s, uc, uni="МГУ", status="hired", revoked=True, points=900, comp=FULL),
        "bot": await lead(db, s, ub, status="hired", points=900, comp=FULL),
        "test": await lead(db, s, ut, status="hired", points=900, comp=FULL),
        "d20": await lead(db, s, u5, uni="МИФИ", status="offer", days_ago=20, points=99, comp=FULL),
        "d60": await lead(db, s, u6, uni="МИФИ", status="rejected", days_ago=60, points=1070, comp=HALF),
    }
    for _ in range(2):
        await event(db, u1, "app_open")
    for who, name, d in [(u1, "game_start", 0), (u1, "lead_form_open", 0), (u4, "app_open", 0), (ub, "app_open", 0), (ut, "app_open", 0),
                         (u2, "session_start", 0), (u2, "game_end", 0), (u3, "finale_view", 0), (u5, "app_open", 20), (u6, "app_open", 60)]:
        await event(db, who, name, d)
    yield hr
    await db.execute(CLEANUP)


def stage(data, name):
    return next(s for s in data["funnel"] if s["stage"] == name)["count"]


# ---------------------------------------------------------------- analytics: access and validation

async def test_analytics_roles(hr):
    assert_error(await hr.get(ANALYTICS), 401, "unauthorized")
    assert_error(await hr.get(ANALYTICS, headers=dev(STRANGER)), 403, "forbidden")
    for role in ROLES:
        r = await hr.get(ANALYTICS, headers=as_(role))
        assert r.status_code == 200, role
        assert r.headers["cache-control"] == "no-store"
    await hr.db.execute("UPDATE hr_users SET active = false WHERE id = $1", hr.ids["analyst"])
    assert_error(await hr.get(ANALYTICS, headers=as_("analyst")), 403, "forbidden")


@pytest.mark.parametrize("period", ["1d", "7", "week", "ALL", "", "30d,7d"])
async def test_analytics_bad_period(hr, period):
    assert_error(await hr.get(ANALYTICS, params={"period": period}, headers=as_("hr")), 422, "validation_error")


async def test_analytics_empty_database(hr):
    d = (await hr.get(ANALYTICS, params={"period": "all"}, headers=as_("analyst"))).json()
    assert set(d) == {"period", "funnel", "statuses", "sources", "points", "competencyAvg", "universities"}
    assert d["period"] == "all" and d["sources"] == [] and d["universities"] == []
    assert [s["stage"] for s in d["funnel"]] == [n for n, _ in an.EVENT_STAGES] + ["lead", "interview", "offer", "hired"]
    assert all(s["count"] == 0 and s["label"] for s in d["funnel"])
    assert d["statuses"] == dict.fromkeys(an.STATUSES, 0) and list(d["statuses"]) == list(an.STATUSES)
    assert [p["count"] for p in d["points"]] == [0] * 8
    assert d["competencyAvg"] == dict.fromkeys(["speed", "accuracy", "attention", "endurance", "reliability", "company"], 0)


async def test_analytics_default_period_is_30d(world):
    d = (await world.get(ANALYTICS, headers=as_("analyst"))).json()
    assert d["period"] == "30d" and d["statuses"]["offer"] == 1 and d["statuses"]["rejected"] == 0


# ---------------------------------------------------------------- analytics: numbers

async def test_analytics_7d(world):
    d = (await world.get(ANALYTICS, params={"period": "7d"}, headers=as_("hr"))).json()
    assert d["period"] == "7d"
    assert d["statuses"] == {"new": 1, "screening": 1, "interview": 1, "offer": 0, "hired": 1, "rejected": 0, "reserve": 0}
    assert [(s["stage"], s["label"], s["count"]) for s in d["funnel"]] == [
        ("app_open", "Открыли игру", 2),  # u1 (two events, one player) and u4; bot / test never
        ("session_start", "Начали сессию (интро)", 1), ("game_start", "Запустили смену", 1), ("game_end", "Завершили смену", 1),
        ("finale_view", "Дошли до финала сезона", 1), ("lead_form_open", "Открыли анкету", 1),
        ("lead", "Оставили анкету", 4), ("interview", "Дошли до интервью", 2), ("offer", "Получили оффер", 1), ("hired", "Приняты", 1)]
    assert d["sources"] == [
        {"source": "tg_a", "players": 3, "leads": 2, "hired": 1},       # u1, u2 and the revoked one are players; the revoked lead is not a lead
        {"source": "qr_b", "players": 1, "leads": 1, "hired": 0},
        {"source": "landing-hr", "players": 0, "leads": 1, "hired": 0},
        {"source": "напрямую", "players": 1, "leads": 0, "hired": 0}]
    assert d["universities"] == [{"name": "МГУ", "leads": 3, "interview": 2, "hired": 1}, {"name": "МГТУ", "leads": 1, "interview": 0, "hired": 0}]
    assert [(p["from"], p["to"], p["count"]) for p in d["points"]] == [
        (0, 99, 1), (100, 199, 1), (200, 299, 0), (300, 399, 0), (400, 499, 0), (500, 599, 0), (600, 699, 0), (700, 799, 1)]
    assert d["competencyAvg"] == {"speed": 70, "accuracy": 50, "attention": 40, "endurance": 10, "reliability": 75, "company": 0}


async def test_analytics_periods_widen(world):
    d30 = (await world.get(ANALYTICS, params={"period": "30d"}, headers=as_("hr"))).json()
    dall = (await world.get(ANALYTICS, params={"period": "all"}, headers=as_("hr"))).json()
    assert d30["statuses"]["offer"] == 1 and d30["statuses"]["rejected"] == 0 and stage(d30, "lead") == 5 and stage(d30, "app_open") == 3
    assert dall["statuses"]["rejected"] == 1 and stage(dall, "lead") == 6 and stage(dall, "app_open") == 4
    assert stage(dall, "offer") == 2 and stage(dall, "interview") == 3  # offer = offer + hired, interview = interview + offer + hired
    assert {s["source"]: s["players"] for s in dall["sources"]}["old_src"] == 1
    assert "old_src" not in {s["source"] for s in d30["sources"]}
    assert dall["points"][0]["count"] == 2 and dall["points"][7]["count"] == 2  # 99 -> first bucket, 1070 -> the open-ended last one
    assert {u["name"] for u in dall["universities"]} == {"МГУ", "МГТУ", "МИФИ"}
    assert {u["name"] for u in d30["universities"]} == {"МГУ", "МГТУ", "МИФИ"}


async def test_analytics_never_counts_bots_tests_revoked(world):
    await world.db.execute("UPDATE leads SET status = 'hired'")
    d = (await world.get(ANALYTICS, params={"period": "all"}, headers=as_("analyst"))).json()
    assert sum(d["statuses"].values()) == 6 and stage(d, "lead") == 6  # 9 leads - revoked - bot - test
    assert sum(p["count"] for p in d["points"]) == 5  # the landing lead has no player row


async def test_analytics_cache_is_shared_for_60_seconds(world):
    first = (await world.get(ANALYTICS, params={"period": "7d"}, headers=as_("analyst"))).json()
    ttl = await world.redis.ttl("hr:analytics:7d")
    assert 0 < ttl <= 60
    await lead(world.db, await world.db.fetchval("SELECT id FROM seasons"), None, uni="КЭС", source="new_src")
    again = (await world.get(ANALYTICS, params={"period": "7d"}, headers=as_("admin"))).json()
    assert again == first and stage(again, "lead") == 4
    other = (await world.get(ANALYTICS, params={"period": "all"}, headers=as_("hr"))).json()  # the key includes the period
    assert stage(other, "lead") == 7
    await world.redis.flushdb()
    fresh = (await world.get(ANALYTICS, params={"period": "7d"}, headers=as_("hr"))).json()
    assert stage(fresh, "lead") == 5 and "new_src" in {s["source"] for s in fresh["sources"]}


async def test_analytics_survives_a_dead_redis(world):
    world.app.state.redis = DeadRedis()
    r = await world.get(ANALYTICS, params={"period": "7d"}, headers=as_("hr"))
    assert r.status_code == 200 and stage(r.json(), "lead") == 4


# ---------------------------------------------------------------- export

def parse(resp) -> tuple[list[str], list[dict[str, str]]]:
    rows = list(csv.reader(io.StringIO(resp.content.decode("utf-8-sig")), delimiter=";"))
    return rows[0], [dict(zip(rows[0], r, strict=True)) for r in rows[1:]]


@pytest_asyncio.fixture
async def book(hr):
    db, s = hr.db, await season(hr.db)
    ua, ub, ubot, utest, urev = [await user(db, 8900 + i, source="tg_x") for i in range(5)]
    await db.execute("UPDATE users SET is_bot = true WHERE id = $1", ubot)
    await db.execute("UPDATE users SET is_test = true WHERE id = $1", utest)
    hr.l = {
        "a": await lead(db, s, ua, name="Иванов Иван", uni="МГУПП", status="interview", tags=["senior"], vacancy="operator",
                        phone="+7 999 123-45-67", assignee=hr.ids["hr_lead"], points=200, comp=FULL,
                        fit='{"operator": {"score": 82, "reasons": []}, "lab": {"score": 40, "reasons": []}}'),
        "b": await lead(db, s, ub, name="=cmd|' /C calc'!A0", uni="МИФИ", status="new", vacancy="lab", points=20,
                        comp='{"speed": 20}', fit='{"lab": {"score": 55, "reasons": []}}'),
        "c": await lead(db, s, None, name="Петров Пётр", uni="МГУПП", status="screening", source="landing-hr"),
        "bot": await lead(db, s, ubot, name="Bot Lead"),
        "test": await lead(db, s, utest, name="Test Lead"),
        "rev": await lead(db, s, urev, name="Revoked Lead", revoked=True),
    }
    hr.names = {v: k for k, v in hr.l.items()}
    yield hr
    await db.execute(CLEANUP)


def ids_of(book, resp) -> set[str]:
    return {book.names[int(r["id"])] for r in parse(resp)[1]}


async def test_export_roles(book):
    assert_error(await book.get(EXPORT), 401, "unauthorized")
    assert_error(await book.get(EXPORT, headers=dev(STRANGER)), 403, "forbidden")
    for role in ("analyst", "hr"):
        assert_error(await book.get(EXPORT, headers=as_(role)), 403, "forbidden")
    assert await book.db.fetchval("SELECT count(*) FROM lead_events WHERE kind = 'export'") == 0
    for role in ("hr_lead", "admin"):
        assert (await book.get(EXPORT, headers=as_(role))).status_code == 200


async def test_export_file_format(book):
    r = await book.get(EXPORT, headers=as_("hr_lead"))
    assert r.headers["content-type"] == "text/csv; charset=utf-8"
    assert re.fullmatch(r'attachment; filename="candidates-\d{4}-\d{2}-\d{2}\.csv"', r.headers["content-disposition"])
    assert r.headers["cache-control"] == "no-store"
    assert r.content.startswith(b"\xef\xbb\xbf") and b"\r\n" in r.content and b";" in r.content.split(b"\r\n")[0]
    head, rows = parse(r)
    assert head == list(exp.COLUMNS) and "ОД" in head and "компетенция: скорость" in head and "лучший fit: балл" in head
    assert len(rows) == 3  # a, b, c: the bot, the test user and the revoked lead are not in the file
    assert [int(x["id"]) for x in rows] == sorted((int(x["id"]) for x in rows), reverse=True)  # newest first
    assert exp.filename().startswith("candidates-")


async def test_export_content(book):
    _, rows = parse(await book.get(EXPORT, headers=as_("hr_lead")))
    by = {book.names[int(r["id"])]: r for r in rows}
    a, b, c = by["a"], by["b"], by["c"]
    assert (a["ФИО"], a["телефон"], a["вуз"], a["статус"], a["теги"]) == ("Иванов Иван", "+7 999 123-45-67", "МГУПП", "interview", "senior")
    assert a["вакансия"] == "Оператор линии"  # the profession title, not the id
    assert a["ОД"] == "200"
    assert [a[f"компетенция: {t}"] for t in ("скорость", "точность", "внимательность", "выносливость", "надёжность", "знание компании")] == \
        ["80", "60", "40", "20", "100", "0"]
    assert (a["лучший fit: профессия"], a["лучший fit: балл"]) == ("Оператор линии", "82")
    assert b["ФИО"] == "'=cmd|' /C calc'!A0"  # formula neutralised
    assert (b["ОД"], b["компетенция: скорость"], b["компетенция: точность"], b["лучший fit: балл"]) == ("20", "20", "", "55")
    assert (c["ОД"], c["компетенция: скорость"], c["лучший fit: балл"], c["источник"]) == ("", "", "", "landing-hr")  # no game profile
    assert a["источник"] == "tg_x"


@pytest.mark.parametrize("query, expected", [
    ("", {"a", "b", "c"}),
    ("status=interview", {"a"}),
    ("status=new&status=screening", {"b", "c"}),
    ("status=contacted", {"c"}),  # the pre-C4 name of screening
    ("university=МГУПП", {"a", "c"}), ("university=мгу", set()), ("university=МИФИ", {"b"}),  # exact name, as in the list
    ("minPoints=100", {"a"}), ("minPoints=0", {"a", "b", "c"}),
    ("minFit=80", {"a"}), ("minFit=50", {"a", "b"}), ("minFit=0", {"a", "b", "c"}),
    ("competency.speed=50", {"a"}), ("competency.speed=10", {"a", "b"}), ("competency.accuracy=1&competency.speed=10", {"a"}),
    ("profession=lab", {"b"}), ("profession=operator", {"a"}), ("profession=kvasovar", set()),
    ("q=иванов", {"a"}), ("q=мифи", {"b"}), ("q=%25", set()), ("q=_", set()),
    ("assignee=me", {"a"}), ("assignee=none", {"b", "c"}),
    ("status=interview&university=МГУПП", {"a"}), ("status=interview&university=МИФИ", set()),
    ("sort=points&limit=1&cursor=zzz", {"a", "b", "c"}), ("foo=bar", {"a", "b", "c"}),  # list-only / unknown parameters are ignored
])
async def test_export_filters(book, query, expected):
    r = await book.get(f"{EXPORT}?{query}", headers=as_("hr_lead"))
    assert r.status_code == 200, r.text
    assert ids_of(book, r) == expected


@pytest.mark.parametrize("query", [
    "", "status=interview", "status=new&status=screening", "university=МГУПП", "minPoints=100", "minFit=50", "competency.speed=10",
    "profession=lab", "q=иванов", "q=3", "assignee=me", "assignee=none", "status=interview&assignee=me&minPoints=1",
])
async def test_export_is_exactly_the_candidate_list(book, query):
    """The file holds the candidates the list shows for the same query string (C4's filters are reused)."""
    listed = (await book.get(f"/api/hr/candidates?{query}&limit=200", headers=as_("hr_lead"))).json()["items"]
    exported = parse(await book.get(f"{EXPORT}?{query}", headers=as_("hr_lead")))[1]
    assert sorted(int(r["id"]) for r in exported) == sorted(i["id"] for i in listed)


@pytest.mark.parametrize("query", [
    "status=bogus", "status=", "profession=pilot", "minPoints=abc", "minPoints=-1", "minFit=101", "minFit=1.5", "competency.charisma=5",
    "competency.speed=x", "competency.speed=101", "assignee=you",
])
async def test_export_rejects_bad_filters(book, query):
    assert_error(await book.get(f"{EXPORT}?{query}", headers=as_("hr_lead")), 422, "validation_error")
    assert await book.db.fetchval("SELECT count(*) FROM lead_events WHERE kind = 'export'") == 0


async def test_export_writes_one_event_per_exported_lead(book):
    r = await book.get(EXPORT, headers=as_("hr_lead"))
    assert r.status_code == 200
    rows = await book.db.fetch("SELECT lead_id, actor_hr_id, payload FROM lead_events WHERE kind = 'export' ORDER BY lead_id")
    assert [x["lead_id"] for x in rows] == sorted([book.l["a"], book.l["b"], book.l["c"]])
    assert {x["actor_hr_id"] for x in rows} == {book.ids["hr_lead"]}
    assert json.loads(rows[0]["payload"]) == {"format": "csv", "count": 3, "filters": {}}
    # a filtered export by an admin: the filter names / harmless values are logged, the free-text query is not
    await book.get(f"{EXPORT}?q=Иванов&status=interview&minPoints=5&competency.speed=3", headers=as_("admin"))
    last = await book.db.fetchrow("SELECT lead_id, actor_hr_id, payload FROM lead_events WHERE kind = 'export' AND actor_hr_id = $1", book.ids["admin"])
    assert last["lead_id"] == book.l["a"]
    payload = json.loads(last["payload"])
    assert payload == {"format": "csv", "count": 1, "filters": {"q": True, "status": ["interview"], "minPoints": 5, "competency": {"speed": 3}}}
    assert "Иванов" not in last["payload"]
    assert await book.db.fetchval("SELECT count(*) FROM lead_events WHERE kind = 'export'") == 4


async def test_export_of_nothing_is_a_header_only_file(book):
    r = await book.get(f"{EXPORT}?status=reserve", headers=as_("hr_lead"))
    assert r.status_code == 200 and parse(r)[1] == [] and parse(r)[0] == list(exp.COLUMNS)
    assert await book.db.fetchval("SELECT count(*) FROM lead_events WHERE kind = 'export'") == 0


async def test_export_helpers():
    assert exp.csv_safe("=1+1") == "'=1+1" and exp.csv_safe("@x") == "'@x" and exp.csv_safe("-5") == "'-5"
    assert exp.csv_safe("+7 (999) 123-45-67") == "+7 (999) 123-45-67" and exp.csv_safe("ok") == "ok" and exp.csv_safe("") == ""
    assert exp.best_fit({"lab": {"score": 55}, "operator": {"score": 82.4}}) == ("operator", "82")
    assert exp.best_fit({}) == ("", "") and exp.best_fit(None) == ("", "")
