"""C4: HR CRM candidates - list (filters, sorts, cursor), card, PATCH with version check, notes, messages, pipeline, roles.
Real Postgres + Redis (skipped when absent). Contract: vendor/frontend/web/src/lib/hr/types.ts."""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from app.achievements import catalog
from tests.helpers import API_DIR, TEST_DB_URL, TEST_REDIS_URL, dev
from tests.test_errors import assert_error

ROLES = ["hr", "hr_lead", "analyst", "admin"]
TG = {"hr": 8101, "hr_lead": 8102, "analyst": 8103, "admin": 8104}
STRANGER = 8199
BASE = datetime(2026, 10, 1, tzinfo=timezone.utc)
ROW_KEYS = {"id", "fullName", "piiMasked", "status", "university", "course", "roleId", "source", "createdAt", "stageChangedAt",
            "assignee", "tags", "achievementPoints", "competency", "fitBest", "seasonRank", "version"}
DETAIL_KEYS = ROW_KEYS | {"phone", "email", "telegram", "birthDate", "readyFrom", "practiceType", "game", "achievements", "fit",
                          "history", "notes", "canMessage", "vacancyFit"}
COMP = ("speed", "accuracy", "attention", "endurance", "reliability", "company")


def as_(role: str) -> dict[str, str]:
    return dev(TG[role], role)


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


@pytest_asyncio.fixture
async def crm(make_client, clean_db, clean_redis, fake_sim):
    """Client + the dataset (see `seed`). `.ids` = {name: lead id}, `.hr` = {role: hr_users.id}, `.db` asyncpg."""
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_enabled=False)
    c.db, c.redis = clean_db, clean_redis
    c.hr = {}
    for role in ROLES:
        c.hr[role] = await c.db.fetchval("INSERT INTO hr_users (tg_id, role) VALUES ($1, $2) RETURNING id", TG[role], role)
    await c.db.execute("INSERT INTO users (tg_id, first_name, last_name) VALUES ($1, 'Марина', 'Соколова')", TG["hr"])
    c.season = await c.db.fetchval("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '30 days', 30) RETURNING id")
    c.ids, c.users, c.created = {}, {}, {}
    await seed(c)
    yield c
    # no leftovers for the tests that run after this file (test_db downgrades the schema, which refuses while public leads exist)
    await c.db.execute("TRUNCATE factories, seasons, users, outbox RESTART IDENTITY CASCADE")


async def add_cand(c, key: str, *, name: str, hours: int, uni: str = "МГУ", course: str | None = "3", vacancy: str | None = "operator",
                   status: str = "new", stars: int | None = 0, points: int | None = None, comp: dict | None = None,
                   fit: dict | None = None, started: bool = True, source: str | None = "tg_futuretoday", tags: list | None = None,
                   assignee: int | None = None, revoked: bool = False, is_bot: bool = False, is_test: bool = False,
                   landing: bool = False, place: int | None = None, quiz: int | None = None, created: datetime | None = None,
                   **extra) -> int:
    """One candidate: user (+ factory when `stars` is not None, + player_scores when points / comp / fit are given) + lead."""
    uid = None
    n = len(c.ids) + 1
    if not landing:
        uid = await c.db.fetchval(
            "INSERT INTO users (tg_id, username, first_name, source, is_bot, is_test, bot_started_at) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id", 7000 + n, f"u{n}", name.split()[0], source, is_bot, is_test,
            datetime.now(timezone.utc) if started else None)
        if stars is not None:
            await c.db.execute(
                "INSERT INTO factories (user_id, season_id, name, state, stars, level, quality_avg, snapshot, is_bot, is_test) "
                "VALUES ($1, $2, 'Plant', '{}'::jsonb, $3, $4, $5, $6::jsonb, $7, $8)", uid, c.season, stars,
                extra.get("level", 1), extra.get("quality", 0.0), json.dumps(extra.get("snapshot", {})), is_bot, is_test)
            if points is not None or comp is not None or fit is not None:
                await c.db.execute(
                    "INSERT INTO player_scores (user_id, season_id, achievement_points, competency, fit) VALUES ($1, $2, $3, $4::jsonb, $5::jsonb)",
                    uid, c.season, points or 0, json.dumps(comp or {}), json.dumps(fit or {}))
    created = created or BASE + timedelta(hours=hours)
    lid = await c.db.fetchval(
        "INSERT INTO leads (user_id, season_id, full_name, birth_date, phone, email, telegram, university, course, vacancy, "
        "ready_from, practice_type, consent, consent_at, consent_text_version, consent_revoked_at, tags, status, assignee_id, "
        "season_place, quiz_score, source, created_at, updated_at, stage_changed_at) "
        "VALUES ($1, $2, $3, '2003-05-17', $4, $5, $6, $7, $8, $9, '2027-03-01', 'paid', true, now(), 'v1', $10, $11, $12, $13, "
        "$14, $15, $16, $17, $17, $17) RETURNING id",
        uid, c.season, name, f"+7999000{n:04d}", f"c{n}@mail.ru", f"tg_{n}user", uni, course, vacancy,
        datetime.now(timezone.utc) if revoked else None, tags or [], status, assignee, place, quiz,
        "land_general_form" if landing else None, created)
    c.ids[key], c.users[key], c.created[key] = lid, uid, created
    return lid


async def seed(c) -> None:
    sp = {"operator": {"score": 82, "reasons": [{"text": "Смены оператора: 10", "delta": 50}, {"text": "Квиз 6/8", "delta": 32}]},
          "lab": {"score": 40, "reasons": []}}
    await add_cand(c, "ivan", name="Иванов Иван", hours=1, stars=500, points=120, started=True, quiz=6, tags=["senior"], level=4, quality=0.9876,
                   comp=dict(speed=80, accuracy=60, attention=50, endurance=40, reliability=70, company=30), fit=sp,
                   snapshot={"skills": {"byProfession": {"operator": {"played": 10, "wins": 7, "avgScore": 0.8123},
                                                         "lab": {"played": 0, "wins": 0, "avgScore": 0}}, "shiftsTotal": 12}})
    await add_cand(c, "anna", name="Петрова Анна", hours=2, uni="РХТУ", vacancy="lab", status="screening", stars=900, points=300,
                   comp=dict(speed=30, accuracy=90), fit={"lab": {"score": 91, "reasons": [{"text": "Лаборатория", "delta": 91}]},
                                                           "kipia": {"score": 70, "reasons": []}}, assignee=c.hr["hr"])
    await add_cand(c, "oleg", name="Сидоров Олег", hours=3, vacancy="kipia", status="interview", stars=100, started=False, course="выпускник")
    await add_cand(c, "maria", name="Козлова Мария", hours=4, uni="МИРЭА", vacancy=None, status="offer", stars=900, points=10,
                   fit={"storekeeper": {"score": 60, "reasons": []}})
    await add_cand(c, "petr", name="Громов Пётр", hours=5, vacancy="operator", status="hired", stars=None, place=9, tags=["приоритет"])
    await add_cand(c, "land", name="Лендинг Кандидат", hours=6, uni="Колледж №1", vacancy="lab", landing=True, source="land_general_form")
    await add_cand(c, "novikov", name="Новиков Артём", hours=1, vacancy="forklift", status="rejected", stars=50, points=120,
                   created=c.created["ivan"])  # same moment as ivan: ties are broken by id
    await add_cand(c, "rus", name="Резервов Рус", hours=8, vacancy="adjuster", status="reserve", stars=40)
    await add_cand(c, "revoked", name="Отозвал Согласие", hours=9, revoked=True, stars=10)
    bot = await add_cand(c, "bot", name="Демо Бот", hours=10, is_bot=True, stars=1000)
    await add_cand(c, "test", name="Тест Игрок", hours=11, is_test=True, stars=5000)
    assert bot


def ids(c, *keys: str) -> list[int]:
    return [c.ids[k] for k in keys]


async def get(c, role: str, path: str, **params):
    return await c.get(path, headers=as_(role), params=params)


async def page_ids(c, role: str = "hr", **params) -> list[int]:
    r = await get(c, role, "/api/hr/candidates", limit=200, **params)
    assert r.status_code == 200, r.text
    return [x["id"] for x in r.json()["items"]]


VISIBLE = ["ivan", "anna", "oleg", "maria", "petr", "land", "novikov", "rus"]


# ---------------------------------------------------------------- migration 0016

def test_migration_maps_statuses_and_backfills():
    """0015 -> 0016 -> 0015 on a scratch database: contacted <-> screening, stage_changed_at backfill, version 1."""
    async def run(sql_fn):
        conn = await asyncpg.connect(TEST_DB_URL.replace("postgresql+asyncpg://", "postgresql://"), timeout=3)
        try:
            return await sql_fn(conn)
        finally:
            await conn.close()

    admin_url = TEST_DB_URL.rsplit("/", 1)[0].replace("postgresql+asyncpg://", "postgresql://") + "/postgres"
    name = "mosprom_test_c4_mig"
    url = TEST_DB_URL.rsplit("/", 1)[0] + "/" + name
    dsn = url.replace("postgresql+asyncpg://", "postgresql://")

    async def prepare():
        conn = await asyncpg.connect(admin_url, timeout=3)
        try:
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
            await conn.execute(f'CREATE DATABASE "{name}"')
        finally:
            await conn.close()

    try:
        asyncio.run(prepare())
    except Exception as e:
        pytest.skip(f"Postgres not reachable ({type(e).__name__})")
    env = {**os.environ, "DATABASE_URL": url}

    def alembic(*args):
        subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, check=True, env=env, capture_output=True)

    async def q(sql, *a):
        conn = await asyncpg.connect(dsn)
        try:
            return await conn.fetch(sql, *a)
        finally:
            await conn.close()

    alembic("upgrade", "0015")
    asyncio.run(q("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now(), 1)"))
    for i, st in enumerate(["new", "contacted", "interview", "rejected", "hired"], 1):
        asyncio.run(q("INSERT INTO leads (season_id, full_name, university, consent, consent_at, consent_text_version, status, created_at, updated_at) "
                      "VALUES (1, $1, 'U', true, now(), 'v', $2, '2026-01-01', '2026-02-02')", f"N{i}", st))
    alembic("upgrade", "0016")
    rows = {r["full_name"]: r for r in asyncio.run(q("SELECT full_name, status, stage_changed_at, version, assignee_id FROM leads"))}
    assert rows["N2"]["status"] == "screening" and rows["N1"]["status"] == "new" and rows["N5"]["status"] == "hired"
    assert rows["N1"]["stage_changed_at"].date().isoformat() == "2026-01-01"  # new: created_at
    assert rows["N2"]["stage_changed_at"].date().isoformat() == "2026-02-02"  # moved: updated_at
    assert all(r["version"] == 1 and r["assignee_id"] is None for r in rows.values())
    with pytest.raises(asyncpg.CheckViolationError):
        asyncio.run(q("UPDATE leads SET status = 'contacted'"))
    for st in ("offer", "reserve", "screening"):
        asyncio.run(q("UPDATE leads SET status = $1 WHERE full_name = 'N1'", st))
    asyncio.run(q("UPDATE leads SET status = 'offer' WHERE full_name = 'N3'"))
    alembic("downgrade", "0015")
    back = {r["full_name"]: r["status"] for r in asyncio.run(q("SELECT full_name, status FROM leads"))}
    assert back["N1"] == "contacted" and back["N2"] == "contacted" and back["N3"] == "interview"
    names = {r["tablename"] for r in asyncio.run(q("SELECT tablename FROM pg_tables WHERE schemaname = 'public'"))}
    assert "lead_notes" not in names


async def test_schema_after_migration(crm):
    cols = {r["column_name"] for r in await crm.db.fetch("SELECT column_name FROM information_schema.columns WHERE table_name = 'leads'")}
    assert {"assignee_id", "stage_changed_at", "version"} <= cols
    idx = {r["indexname"] for r in await crm.db.fetch("SELECT indexname FROM pg_indexes WHERE tablename IN ('leads', 'lead_notes')")}
    assert {"ix_leads_created_id", "ix_leads_status_stage", "ix_leads_assignee", "ix_leads_user_id", "ix_lead_notes_lead_created"} <= idx
    with pytest.raises(asyncpg.CheckViolationError):
        await crm.db.execute("UPDATE leads SET status = 'contacted' WHERE id = $1", crm.ids["ivan"])
    await crm.db.execute("UPDATE leads SET status = 'reserve' WHERE id = $1", crm.ids["ivan"])


# ---------------------------------------------------------------- list: shape, visibility, roles

async def test_list_shape_and_visibility(crm):
    r = await get(crm, "hr", "/api/hr/candidates")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"items", "nextCursor", "total"} and body["nextCursor"] is None and body["total"] == len(VISIBLE)
    got = {x["id"] for x in body["items"]}
    assert got == set(ids(crm, *VISIBLE))  # revoked consent, demo bot and test player are not candidates
    ivan = next(x for x in body["items"] if x["id"] == crm.ids["ivan"])
    assert set(ivan) == ROW_KEYS
    assert ivan == {
        "id": crm.ids["ivan"], "fullName": "Иванов Иван", "piiMasked": False, "status": "new", "university": "МГУ", "course": "3",
        "roleId": "operator", "source": "tg_futuretoday", "createdAt": ms(BASE + timedelta(hours=1)),
        "stageChangedAt": ms(BASE + timedelta(hours=1)), "assignee": None, "tags": ["senior"], "achievementPoints": 120,
        "competency": dict(speed=80, accuracy=60, attention=50, endurance=40, reliability=70, company=30),
        "fitBest": {"profession": "operator", "score": 82}, "seasonRank": 4, "version": 1}


async def test_row_fallbacks_and_landing_candidates(crm):
    items = {x["id"]: x for x in (await get(crm, "hr", "/api/hr/candidates", limit=200)).json()["items"]}
    anna = items[crm.ids["anna"]]
    assert anna["assignee"] == {"id": crm.hr["hr"], "name": "hr"} and anna["seasonRank"] == 2  # name = the player row (the dev login renamed it)
    assert anna["competency"] == dict(speed=30, accuracy=90, attention=0, endurance=0, reliability=0, company=0)
    maria = items[crm.ids["maria"]]  # no vacancy: the role comes from the best fit
    assert maria["roleId"] == "storekeeper" and maria["fitBest"] == {"profession": "storekeeper", "score": 60}
    petr = items[crm.ids["petr"]]  # no factory: rank falls back to the place stored at submission; no score row: zeros
    assert petr["seasonRank"] == 9 and petr["achievementPoints"] == 0 and petr["fitBest"] is None
    assert set(petr["competency"]) == set(COMP) and not any(petr["competency"].values())
    land = items[crm.ids["land"]]  # landing intake: no game account at all
    assert land["source"] == "land_general_form" and land["seasonRank"] is None and land["roleId"] == "lab"
    assert land["achievementPoints"] == 0 and land["fitBest"] is None and land["fullName"] == "Лендинг Кандидат"
    assert items[crm.ids["oleg"]]["course"] == "выпускник"


async def test_auth_and_roles_on_reads(crm):
    for path in ("/api/hr/candidates", f"/api/hr/candidates/{crm.ids['ivan']}", "/api/hr/pipeline"):
        assert (await crm.get(path)).status_code == 401
        r = await crm.get(path, headers=dev(STRANGER, "x"))
        assert_error(r, 403, "forbidden")
        for role in ROLES:  # every active HR role reads
            r = await get(crm, role, path)
            assert r.status_code == 200 and r.headers["cache-control"] == "no-store", (role, path)
    await crm.db.execute("UPDATE hr_users SET active = false WHERE id = $1", crm.hr["hr"])
    assert_error(await get(crm, "hr", "/api/hr/candidates"), 403, "forbidden")  # deactivation is immediate


async def test_analyst_sees_masked_rows(crm):
    items = (await get(crm, "analyst", "/api/hr/candidates", limit=200)).json()["items"]
    assert len(items) == len(VISIBLE)
    for x in items:
        assert x["piiMasked"] is True and x["fullName"] == f"Кандидат #{x['id']}"
        assert x["university"] and x["achievementPoints"] >= 0  # the rest stays
    assert "Иванов" not in json.dumps(items, ensure_ascii=False)
    hr_items = (await get(crm, "hr_lead", "/api/hr/candidates", limit=200)).json()["items"]
    assert all(x["piiMasked"] is False for x in hr_items) and any(x["fullName"] == "Иванов Иван" for x in hr_items)


# ---------------------------------------------------------------- list: filters

async def test_filter_status(crm):
    assert set(await page_ids(crm, status="new")) == set(ids(crm, "ivan", "land"))
    assert set(await page_ids(crm, status=["new", "hired"])) == set(ids(crm, "ivan", "land", "petr"))
    assert set(await page_ids(crm, status="screening")) == set(ids(crm, "anna"))
    assert set(await page_ids(crm, status="contacted")) == set(ids(crm, "anna"))  # the pre-C4 name
    assert set(await page_ids(crm, status=["offer", "reserve", "rejected", "interview"])) == set(ids(crm, "maria", "rus", "novikov", "oleg"))
    assert_error(await get(crm, "hr", "/api/hr/candidates", status="bogus"), 422, "validation_error")


async def test_filter_profession_university_points_fit_competency_assignee(crm):
    assert set(await page_ids(crm, profession="lab")) == set(ids(crm, "anna", "land"))  # vacancy lab OR best fit lab
    assert set(await page_ids(crm, profession="storekeeper")) == set(ids(crm, "maria"))  # via the best fit
    assert set(await page_ids(crm, profession="operator")) == set(ids(crm, "ivan", "petr"))
    assert_error(await get(crm, "hr", "/api/hr/candidates", profession="pilot"), 422, "validation_error")
    assert set(await page_ids(crm, university="МГУ")) == set(ids(crm, "ivan", "oleg", "petr", "novikov", "rus"))
    assert await page_ids(crm, university="мгу") == []  # exact match, as in the client's select
    assert set(await page_ids(crm, minPoints=120)) == set(ids(crm, "anna", "ivan", "novikov"))
    assert set(await page_ids(crm, minPoints=0)) == set(ids(crm, *VISIBLE))
    assert set(await page_ids(crm, minFit=70)) == set(ids(crm, "ivan", "anna"))
    assert set(await page_ids(crm, minFit=91)) == set(ids(crm, "anna"))
    assert set(await page_ids(crm, **{"competency.speed": 70})) == set(ids(crm, "ivan"))
    assert set(await page_ids(crm, **{"competency.accuracy": 60})) == set(ids(crm, "ivan", "anna"))
    assert set(await page_ids(crm, **{"competency.speed": 0})) == set(ids(crm, *VISIBLE))
    r = await crm.get("/api/hr/candidates?competency.accuracy=60&competency.speed=70", headers=as_("hr"))
    assert [x["id"] for x in r.json()["items"]] == ids(crm, "ivan")  # several competencies are ANDed
    for bad in ("competency.humor=5", "competency.speed=101", "competency.speed=x", "minFit=101", "minPoints=-1", "assignee=you", "sort=name"):
        assert_error(await crm.get(f"/api/hr/candidates?{bad}", headers=as_("hr")), 422, "validation_error")
    assert set(await page_ids(crm, "hr", assignee="me")) == set(ids(crm, "anna"))
    assert await page_ids(crm, "hr_lead", assignee="me") == []
    assert set(await page_ids(crm, "hr_lead", assignee="none")) == set(ids(crm, *VISIBLE)) - set(ids(crm, "anna"))
    both = await page_ids(crm, status="screening", profession="lab", minFit=80, assignee="me")  # filters combine with AND
    assert both == ids(crm, "anna")
    assert await page_ids(crm, status="new", profession="lab", minFit=80) == []


async def test_search(crm):
    assert await page_ids(crm, q="Иванов") == ids(crm, "ivan")
    assert await page_ids(crm, q="  иванов ") == ids(crm, "ivan")  # trimmed, case-insensitive
    assert set(await page_ids(crm, q="мгу")) == set(ids(crm, "ivan", "oleg", "petr", "novikov", "rus"))
    assert await page_ids(crm, q=str(crm.ids["maria"])) == ids(crm, "maria")  # exact id
    assert await page_ids(crm, q="c2@mail.ru") == ids(crm, "anna")  # contacts: PII roles only
    assert await page_ids(crm, q="%") == [] and await page_ids(crm, q="Ив_нов") == []  # wildcards are literals
    # the analyst cannot find people by name / contact (the search would unmask them), only by university or id
    assert await page_ids(crm, "analyst", q="Иванов") == []
    assert await page_ids(crm, "analyst", q="c2@mail.ru") == []
    assert set(await page_ids(crm, "analyst", q="МГУ")) == set(ids(crm, "ivan", "oleg", "petr", "novikov", "rus"))
    assert await page_ids(crm, "analyst", q=str(crm.ids["maria"])) == ids(crm, "maria")
    assert_error(await get(crm, "hr", "/api/hr/candidates", q="x" * 101), 422, "validation_error")


# ---------------------------------------------------------------- list: sorts and the cursor

def expected_order(c, sort: str) -> list[int]:
    created = {k: c.created[k] for k in VISIBLE}
    pts = {"ivan": 120, "anna": 300, "maria": 10, "novikov": 120}
    fit = {"ivan": 82, "anna": 91, "maria": 60}
    rank = {"anna": 2, "maria": 3, "ivan": 4, "oleg": 5, "novikov": 6, "rus": 7, "petr": 9}
    if sort == "fresh":
        keys = sorted(VISIBLE, key=lambda k: (created[k], c.ids[k]), reverse=True)
    elif sort == "points":
        keys = sorted(VISIBLE, key=lambda k: (pts.get(k, 0), c.ids[k]), reverse=True)
    elif sort == "fit":
        keys = sorted(VISIBLE, key=lambda k: (fit.get(k, 0), c.ids[k]), reverse=True)
    else:
        keys = sorted(VISIBLE, key=lambda k: (rank.get(k, 10**9), c.ids[k]))
    return ids(c, *keys)


@pytest.mark.parametrize("sort", ["fresh", "points", "fit", "rank"])
async def test_sorts_and_cursor_walk(crm, sort):
    want = expected_order(crm, sort)
    assert await page_ids(crm, sort=sort) == want
    assert await page_ids(crm) == expected_order(crm, "fresh")  # default sort = fresh
    for limit in (1, 3, len(VISIBLE)):  # walk the pages: no gaps, no repeats, stable total
        seen, cursor, pages = [], None, 0
        while True:
            params = {"sort": sort, "limit": limit, **({"cursor": cursor} if cursor else {})}
            body = (await get(crm, "hr", "/api/hr/candidates", **params)).json()
            assert body["total"] == len(VISIBLE) and len(body["items"]) <= limit
            seen += [x["id"] for x in body["items"]]
            pages += 1
            cursor = body["nextCursor"]
            if cursor is None:
                break
            assert pages <= len(VISIBLE)
        assert seen == want, (sort, limit)
        assert pages == -(-len(VISIBLE) // limit)  # the last full page has no dangling cursor


async def test_cursor_is_opaque_and_validated(crm):
    page = (await get(crm, "hr", "/api/hr/candidates", limit=2)).json()
    assert isinstance(page["nextCursor"], str) and page["nextCursor"]
    for bad in ("zzz", "e30", "W10", "x" * 150):
        assert_error(await get(crm, "hr", "/api/hr/candidates", cursor=bad), 422, "validation_error")
    # a cursor belongs to its sort
    assert_error(await get(crm, "hr", "/api/hr/candidates", cursor=page["nextCursor"], sort="points"), 422, "validation_error")
    for bad_limit in (0, 201, -1):
        assert_error(await get(crm, "hr", "/api/hr/candidates", limit=bad_limit), 422, "validation_error")
    # filters apply on every page: the total counts the whole selection, the cursor continues inside it
    first = (await get(crm, "hr", "/api/hr/candidates", status=["new", "hired"], limit=2)).json()
    assert first["total"] == 3 and len(first["items"]) == 2 and first["nextCursor"]
    rest = (await get(crm, "hr", "/api/hr/candidates", status=["new", "hired"], limit=2, cursor=first["nextCursor"])).json()
    assert len(rest["items"]) == 1 and rest["nextCursor"] is None
    assert all(x["status"] in ("new", "hired") for x in first["items"] + rest["items"])


async def test_many_candidates_paginate(crm):
    await crm.db.execute(
        "INSERT INTO leads (season_id, full_name, university, consent, consent_at, consent_text_version, created_at, updated_at, "
        "stage_changed_at) SELECT $1, 'Массовый ' || g, 'U', true, now(), 'v', $2::timestamptz + (g % 7) * interval '1 hour', now(), now() "
        "FROM generate_series(1, 120) g", crm.season, BASE)
    total = 120 + len(VISIBLE)
    for sort in ("fresh", "points", "fit", "rank"):
        seen, cursor = [], None
        while True:
            body = (await get(crm, "hr", "/api/hr/candidates", sort=sort, limit=50, **({"cursor": cursor} if cursor else {}))).json()
            seen += [x["id"] for x in body["items"]]
            cursor = body["nextCursor"]
            if not cursor:
                break
        assert len(seen) == len(set(seen)) == total == body["total"], sort


# ---------------------------------------------------------------- card

async def test_detail_shape_and_content(crm):
    await crm.db.execute("INSERT INTO achievements_earned (user_id, achievement_id, tier, points, evidence, catalog_version, granted_via) "
                         "VALUES ($1, 'bottles_1k', 'bronze', 10, '{\"produced\": 1500}', 1, 'hook'), "
                         "($1, 'first_line', 'bronze', 10, '{\"quest\": \"q_line\"}', 1, 'hook')", crm.users["ivan"])
    await crm.db.execute("INSERT INTO achievement_progress (user_id, achievement_id, current, target) VALUES ($1, 'bottles_10k', 4000, 10000)",
                         crm.users["ivan"])
    await crm.db.execute("INSERT INTO lead_events (lead_id, actor_hr_id, kind, payload) VALUES ($1, $2, 'status', '{\"from\": \"new\", \"to\": \"screening\"}'), "
                         "($1, NULL, 'achievement', '{\"id\": \"first_line\", \"title\": \"Первая линия\", \"points\": 10, \"tier\": \"bronze\"}')",
                         crm.ids["ivan"], crm.hr["hr"])
    await crm.db.execute("INSERT INTO lead_notes (lead_id, author_hr_id, text) VALUES ($1, $2, 'Звонила, удобно во вторник')",
                         crm.ids["ivan"], crm.hr["hr"])
    vid = await crm.db.fetchval("INSERT INTO vacancies (profession_id, title, requirements) VALUES ('operator', 'Оператор линии', "
                                "'{\"competencies\": {\"speed\": 60}, \"achievements\": []}') RETURNING id")
    r = await get(crm, "hr_lead", f"/api/hr/candidates/{crm.ids['ivan']}")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    d = r.json()
    assert d["vacancyFit"] == [{"vacancyId": vid, "professionId": "operator", "title": "Оператор линии", "active": True, "score": 100,
                                "reasons": d["vacancyFit"][0]["reasons"]}]  # extra to the client contract: fit to the open vacancies
    assert set(d) == DETAIL_KEYS and d["piiMasked"] is False
    assert d["fullName"] == "Иванов Иван" and d["phone"] == "+79990000001" and d["email"] == "c1@mail.ru"
    assert d["telegram"] == "@tg_1user" and d["birthDate"] == "2003-05-17" and d["readyFrom"] == "2027-03-01" and d["practiceType"] == "paid"
    assert d["canMessage"] is True and d["seasonRank"] == 4 and d["version"] == 1
    assert d["game"] == {"level": 4, "stars": 500, "quality": 99, "shiftsTotal": 12, "quizScore": 6,
                         "byProfession": [{"profession": "operator", "played": 10, "wins": 7, "avgScore": 0.81}]}
    # achievements: the whole catalog, earned ones with date + evidence, the rest with progress where there is a bar
    ach = {a["id"]: a for a in d["achievements"]}
    assert [a["id"] for a in d["achievements"]] == [a.id for a in catalog.CATALOG]
    assert set(ach["bottles_1k"]) == set(catalog.CATALOG[0].public()) | {"earnedAt", "progress", "evidence"}
    assert ach["bottles_1k"]["earnedAt"] > 0 and ach["bottles_1k"]["evidence"] == "выпущено бутылок: 1500" and ach["bottles_1k"]["progress"] is None
    assert ach["first_line"]["evidence"] is None and ach["first_line"]["earnedAt"]
    assert ach["bottles_10k"] == {**catalog.BY_ID["bottles_10k"].public(), "earnedAt": None, "evidence": None,
                                  "progress": {"current": 4000, "target": 10000}}
    assert ach["sku_all"]["earnedAt"] is None and ach["sku_all"]["progress"] is None
    assert any(a["hidden"] for a in d["achievements"])
    # fit: best first, with the reasons C2 wrote
    assert d["fit"] == [
        {"profession": "operator", "title": "Оператор линии розлива", "score": 82,
         "reasons": [{"text": "Смены оператора: 10", "delta": 50}, {"text": "Квиз 6/8", "delta": 32}]},
        {"profession": "lab", "title": "Лаборант ОТК", "score": 40, "reasons": []}]
    assert d["competency"] == dict(speed=80, accuracy=60, attention=50, endurance=40, reliability=70, company=30)
    # history: newest first, the card view just written, the system / game actors, the form itself at the bottom
    kinds = [e["kind"] for e in d["history"]]
    assert kinds == ["view", "achievement", "status", "status"]
    view, ach_ev, st_ev, created = d["history"]
    assert view["payload"] == {"masked": False} and view["actor"] == "hr_lead"  # the player row the dev login created
    assert ach_ev["actor"] == "игра" and ach_ev["payload"]["title"] == "Первая линия"
    assert st_ev["actor"] == "Марина Соколова" and st_ev["payload"] == {"from": "new", "to": "screening"}
    assert created["payload"] == {"to": "new"} and created["actor"] == "система" and created["at"] == ms(BASE + timedelta(hours=1))
    assert set(view) == {"id", "kind", "at", "actor", "payload"} and isinstance(view["at"], int)
    assert d["notes"] == [{"id": d["notes"][0]["id"], "author": "Марина Соколова", "text": "Звонила, удобно во вторник",
                           "at": d["notes"][0]["at"]}]


async def test_detail_without_game_account(crm):
    d = (await get(crm, "hr", f"/api/hr/candidates/{crm.ids['land']}")).json()
    assert set(d) == DETAIL_KEYS and d["fullName"] == "Лендинг Кандидат" and d["canMessage"] is False and d["source"] == "land_general_form"
    assert d["game"] == {"level": 0, "stars": 0, "quality": 0, "shiftsTotal": 0, "quizScore": None, "byProfession": []}
    assert d["fit"] == [] and len(d["achievements"]) == len(catalog.CATALOG) and not any(a["earnedAt"] for a in d["achievements"])
    assert d["history"][-1]["payload"] == {"to": "new"} and d["vacancyFit"] == []
    # a player with a factory but no scores yet (C2 columns empty): empty objects, zeros
    d = (await get(crm, "hr", f"/api/hr/candidates/{crm.ids['oleg']}")).json()
    assert d["competency"] == {k: 0 for k in COMP} and d["fit"] == [] and d["fitBest"] is None and d["canMessage"] is False


async def test_view_is_logged_and_hidden_cards_are_404(crm):
    lid = crm.ids["anna"]
    for role in ROLES:
        assert (await get(crm, role, f"/api/hr/candidates/{lid}")).status_code == 200
    rows = await crm.db.fetch("SELECT actor_hr_id, payload FROM lead_events WHERE kind = 'view' AND lead_id = $1 ORDER BY id", lid)
    assert [r["actor_hr_id"] for r in rows] == [crm.hr[r] for r in ROLES]  # one row per view, with the viewer
    assert [json.loads(r["payload"]) for r in rows] == [{"masked": False}, {"masked": False}, {"masked": True}, {"masked": False}]
    for key in ("revoked", "bot", "test"):
        assert_error(await get(crm, "hr", f"/api/hr/candidates/{crm.ids[key]}"), 404, "lead_not_found")
        assert await crm.db.fetchval("SELECT count(*) FROM lead_events WHERE lead_id = $1", crm.ids[key]) == 0
    assert_error(await get(crm, "hr", "/api/hr/candidates/999999"), 404, "lead_not_found")
    assert_error(await get(crm, "hr", "/api/hr/candidates/0"), 422, "validation_error")
    assert_error(await get(crm, "hr", "/api/hr/candidates/abc"), 422, "validation_error")


async def test_analyst_card_is_masked_without_notes(crm):
    lid = crm.ids["ivan"]
    await crm.db.execute("INSERT INTO lead_notes (lead_id, author_hr_id, text) VALUES ($1, $2, 'секретная заметка')", lid, crm.hr["hr"])
    d = (await get(crm, "analyst", f"/api/hr/candidates/{lid}")).json()
    assert d["piiMasked"] is True and d["fullName"] == f"Кандидат #{lid}"
    assert d["phone"] is None and d["email"] is None and d["telegram"] is None and d["birthDate"] is None
    assert d["notes"] == []
    text = json.dumps(d, ensure_ascii=False)
    for secret in ("Иванов", "секретная", "c1@mail.ru", "+7999", "tg_1user", "2003-05-17"):
        assert secret not in text, secret
    assert d["game"]["level"] == 4 and d["achievements"] and d["fit"] and d["history"]  # the game side stays visible
    assert d["history"][0]["payload"] == {"masked": True}
    hr = (await get(crm, "hr", f"/api/hr/candidates/{lid}")).json()
    assert hr["notes"][0]["text"] == "секретная заметка"


# ---------------------------------------------------------------- PATCH

async def patch(c, role, key, body, expect=200):
    r = await c.patch(f"/api/hr/candidates/{c.ids[key]}", headers=as_(role), json=body)
    assert r.status_code == expect, r.text
    return r


async def events(c, key, kind=None):
    rows = await c.db.fetch("SELECT actor_hr_id, kind, payload FROM lead_events WHERE lead_id = $1 " + ("AND kind = $2 " if kind else "") + "ORDER BY id",
                            *([c.ids[key], kind] if kind else [c.ids[key]]))
    return [(r["actor_hr_id"], r["kind"], json.loads(r["payload"])) for r in rows]


async def test_patch_status_bumps_version_and_logs(crm):
    before = await crm.db.fetchrow("SELECT stage_changed_at, updated_at FROM leads WHERE id = $1", crm.ids["ivan"])
    r = await patch(crm, "hr", "ivan", {"status": "interview", "version": 1})
    row = r.json()
    assert set(row) == ROW_KEYS and row["status"] == "interview" and row["version"] == 2 and row["piiMasked"] is False
    assert row["stageChangedAt"] > ms(before["stage_changed_at"]) and row["id"] == crm.ids["ivan"]
    assert (await events(crm, "ivan", "status")) == [(crm.hr["hr"], "status", {"from": "new", "to": "interview", "via": "web"})]
    # the same status again: nothing changes, no event, version stays (a harmless no-op)
    row = (await patch(crm, "hr", "ivan", {"status": "interview", "version": 2})).json()
    assert row["version"] == 2 and len(await events(crm, "ivan", "status")) == 1
    # any move is allowed (kanban drag and drop), including back
    assert (await patch(crm, "hr_lead", "ivan", {"status": "new", "version": 2})).json()["version"] == 3
    for st in ("screening", "offer", "hired", "rejected", "reserve"):
        v = await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", crm.ids["ivan"])
        assert (await patch(crm, "admin", "ivan", {"status": st, "version": v})).json()["status"] == st
    assert (await patch(crm, "hr", "anna", {"status": "contacted", "version": 1})).json()["status"] == "screening"  # old name


async def test_patch_validation(crm):
    lid = crm.ids["ivan"]
    for body in ({"version": 1}, {}, {"status": None, "version": 1}, {"tags": None, "version": 1}, {"status": "bogus", "version": 1},
                 {"status": "new"}, {"status": "new", "version": -1}, {"status": "new", "version": "x"},
                 {"tags": ["a"] * 21, "version": 1}, {"tags": ["x" * 41], "version": 1}, {"tags": [""], "version": 1}, {"tags": "a", "version": 1},
                 {"assigneeId": 0, "version": 1}, {"assigneeId": "me", "version": 1}):
        assert_error(await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr"), json=body), 422, "validation_error")
    assert (await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", lid)) == 1
    r = await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr"), content=b"[]")
    assert r.status_code == 422


async def test_patch_version_conflict(crm):
    lid = crm.ids["ivan"]
    await patch(crm, "hr", "ivan", {"status": "screening", "version": 1})
    r = await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr_lead"), json={"status": "interview", "version": 1})
    assert_error(r, 409, "version_conflict")
    assert r.json()["error"]["details"] == {"version": 2}
    assert (await crm.db.fetchval("SELECT status FROM leads WHERE id = $1", lid)) == "screening"  # the loser changed nothing
    assert len(await events(crm, "ivan", "status")) == 1
    assert_error(await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr"), json={"status": "offer", "version": 9}), 409, "version_conflict")
    await patch(crm, "hr_lead", "ivan", {"status": "interview", "version": 2})


async def test_patch_concurrent_writers_exactly_one_wins(crm):
    lid = crm.ids["ivan"]
    rs = await asyncio.gather(*[crm.patch(f"/api/hr/candidates/{lid}", headers=as_(role), json={"status": st, "version": 1})
                                for role, st in (("hr", "screening"), ("hr_lead", "interview"), ("admin", "offer"))])
    assert sorted(r.status_code for r in rs) == [200, 409, 409], [r.text for r in rs]
    assert (await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", lid)) == 2


async def test_patch_assignee(crm):
    lid = crm.ids["ivan"]
    row = (await patch(crm, "hr_lead", "ivan", {"assigneeId": crm.hr["hr"], "version": 1})).json()
    assert row["assignee"] == {"id": crm.hr["hr"], "name": "Марина Соколова"} and row["version"] == 2
    assert (await events(crm, "ivan", "assign")) == [(crm.hr["hr_lead"], "assign", {
        "from": "никто", "to": "Марина Соколова", "fromId": None, "toId": crm.hr["hr"]})]
    assert (await get(crm, "hr", "/api/hr/candidates", assignee="me")).json()["total"] == 2  # ivan and anna
    row = (await patch(crm, "hr_lead", "ivan", {"assigneeId": None, "version": 2})).json()  # explicit null = unassign
    assert row["assignee"] is None and row["version"] == 3
    assert (await events(crm, "ivan", "assign"))[-1][2]["to"] == "никто"
    row = (await patch(crm, "hr_lead", "ivan", {"assigneeId": None, "version": 3})).json()  # already unassigned: no-op
    assert row["version"] == 3
    # only an active writer can be responsible: not an analyst, not a deactivated user, not an unknown id
    await crm.db.execute("UPDATE hr_users SET active = false WHERE id = $1", crm.hr["admin"])
    for bad in (crm.hr["analyst"], crm.hr["admin"], 999999):
        assert_error(await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr"), json={"assigneeId": bad, "version": 3}), 422, "validation_error")
    assert (await crm.db.fetchval("SELECT assignee_id FROM leads WHERE id = $1", lid)) is None
    assert (await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", lid)) == 3  # a failed patch writes nothing
    # an HR user without a player row is named by id
    row = (await patch(crm, "hr", "ivan", {"assigneeId": crm.hr["hr_lead"], "version": 3})).json()
    assert row["assignee"] == {"id": crm.hr["hr_lead"], "name": "hr_lead"}  # the player row the dev login created


async def test_patch_tags(crm):
    row = (await patch(crm, "hr", "ivan", {"tags": [" общежитие ", "senior", "общежитие", "приоритет"], "version": 1})).json()
    assert row["tags"] == ["общежитие", "senior", "приоритет"] and row["version"] == 2  # trimmed, de-duplicated, order kept
    assert (await events(crm, "ivan", "tag")) == [(crm.hr["hr"], "tag", {
        "tags": ["общежитие", "senior", "приоритет"], "added": ["общежитие", "приоритет"], "removed": []})]
    row = (await patch(crm, "hr", "ivan", {"tags": [], "version": 2})).json()
    assert row["tags"] == [] and (await events(crm, "ivan", "tag"))[-1][2]["removed"] == ["общежитие", "senior", "приоритет"]
    assert (await patch(crm, "hr", "ivan", {"tags": [], "version": 3})).json()["version"] == 3  # unchanged: no-op
    # several fields in one request: one version bump, one event each
    row = (await patch(crm, "hr", "ivan", {"status": "offer", "tags": ["x"], "assigneeId": crm.hr["hr"], "version": 3})).json()
    assert row["version"] == 4 and row["status"] == "offer" and row["tags"] == ["x"] and row["assignee"]["id"] == crm.hr["hr"]
    assert {k for _, k, _ in await events(crm, "ivan")} == {"status", "assign", "tag"}


async def test_patch_roles_and_hidden_leads(crm):
    lid = crm.ids["ivan"]
    r = await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("analyst"), json={"status": "offer", "version": 1})
    assert_error(r, 403, "forbidden")
    assert_error(await crm.patch(f"/api/hr/candidates/{lid}", headers=dev(STRANGER, "x"), json={"status": "offer", "version": 1}), 403, "forbidden")
    assert (await crm.patch(f"/api/hr/candidates/{lid}", json={"status": "offer", "version": 1})).status_code == 401
    assert (await crm.db.fetchval("SELECT status FROM leads WHERE id = $1", lid)) == "new"
    for role in ("hr", "hr_lead", "admin"):
        await patch(crm, role, "novikov", {"tags": [role], "version": (await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", crm.ids["novikov"]))})
    for key in ("revoked", "bot", "test"):
        r = await crm.patch(f"/api/hr/candidates/{crm.ids[key]}", headers=as_("hr"), json={"status": "offer", "version": 1})
        assert_error(r, 404, "lead_not_found")
    assert_error(await crm.patch("/api/hr/candidates/999999", headers=as_("hr"), json={"status": "offer", "version": 1}), 404, "lead_not_found")


# ---------------------------------------------------------------- notes

async def test_notes(crm):
    lid = crm.ids["ivan"]
    r = await crm.post(f"/api/hr/candidates/{lid}/notes", headers=as_("hr"), json={"text": "  Перезвонить в пятницу  "})
    assert r.status_code == 201 and r.headers["cache-control"] == "no-store"
    note = r.json()
    assert set(note) == {"id", "author", "text", "at"} and note["text"] == "Перезвонить в пятницу" and note["author"] == "hr"
    assert isinstance(note["at"], int)
    await crm.post(f"/api/hr/candidates/{lid}/notes", headers=as_("hr_lead"), json={"text": "Второй"})
    d = (await get(crm, "hr", f"/api/hr/candidates/{lid}")).json()
    assert [n["text"] for n in d["notes"]] == ["Второй", "Перезвонить в пятницу"]  # newest first
    ev = await events(crm, "ivan", "note")
    assert [e[0] for e in ev] == [crm.hr["hr"], crm.hr["hr_lead"]] and ev[0][2] == {"noteId": note["id"]}
    assert "Перезвонить" not in json.dumps([e[2] for e in ev], ensure_ascii=False)  # the text never goes to the event log
    assert (await crm.db.fetchval("SELECT version FROM leads WHERE id = $1", lid)) == 1  # notes do not take part in the version
    assert (await get(crm, "analyst", f"/api/hr/candidates/{lid}")).json()["notes"] == []
    for bad in ({"text": ""}, {"text": "   "}, {"text": "x" * 4001}, {}, {"text": 5}, {"text": "a\x00b"}):
        assert_error(await crm.post(f"/api/hr/candidates/{lid}/notes", headers=as_("hr"), json=bad), 422, "validation_error")
    assert_error(await crm.post(f"/api/hr/candidates/{lid}/notes", headers=as_("analyst"), json={"text": "x"}), 403, "forbidden")
    assert (await crm.post(f"/api/hr/candidates/{lid}/notes", json={"text": "x"})).status_code == 401
    for key in ("revoked", "bot", "test"):
        assert_error(await crm.post(f"/api/hr/candidates/{crm.ids[key]}/notes", headers=as_("hr"), json={"text": "x"}), 404, "lead_not_found")
    assert_error(await crm.post("/api/hr/candidates/999999/notes", headers=as_("hr"), json={"text": "x"}), 404, "lead_not_found")
    assert await crm.db.fetchval("SELECT count(*) FROM lead_notes") == 2


# ---------------------------------------------------------------- messages

async def outbox_messages(c):
    return [(r["id"], json.loads(r["payload"])) for r in await c.db.fetch("SELECT id, payload FROM outbox WHERE kind = 'hr_message' ORDER BY id")]


async def test_message_goes_to_the_outbox(crm):
    lid = crm.ids["ivan"]
    r = await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("hr"),
                       json={"template": "interview", "text": "  Приглашаем на интервью во вторник  "})
    assert r.status_code == 202 and r.headers["cache-control"] == "no-store"
    msgs = await outbox_messages(crm)
    assert r.json() == {"ok": True, "id": msgs[0][0]}
    assert msgs[0][1] == {"lead_id": lid, "user_id": crm.users["ivan"], "tg_id": 7001, "template": "interview",
                          "text": "Приглашаем на интервью во вторник", "first_name": "Иванов", "vacancy": "operator", "hr_user_id": crm.hr["hr"]}
    assert await crm.db.fetchval("SELECT sent_at FROM outbox WHERE id = $1", msgs[0][0]) is None  # the bot delivers it
    ev = await events(crm, "ivan", "message")
    assert ev == [(crm.hr["hr"], "message", {"template": "interview", "outboxId": msgs[0][0]})]
    assert "Приглашаем" not in json.dumps(ev, ensure_ascii=False)
    # no text: the bot's default text of the template; custom needs one
    for tpl in ("offer", "reject"):
        assert (await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("hr_lead"), json={"template": tpl})).status_code == 202
        assert (await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("admin"), json={"template": tpl, "text": "  "})).status_code == 202
    assert [m[1]["text"] for m in (await outbox_messages(crm))[1:]] == [None] * 4
    assert (await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("hr"), json={"template": "custom", "text": "Привет!"})).status_code == 202
    for bad in ({"template": "custom"}, {"template": "custom", "text": "  "}, {"template": "spam", "text": "x"}, {"text": "x"},
                {"template": "offer", "text": "x" * 3501}, {"template": "offer", "text": 5}):
        assert_error(await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("hr"), json=bad), 422, "validation_error")
    assert len(await outbox_messages(crm)) == 6 and len(await events(crm, "ivan", "message")) == 6
    # the message does not move the pipeline
    assert (await crm.db.fetchval("SELECT status || version FROM leads WHERE id = $1", lid)) == "new1"


async def test_message_needs_a_started_bot(crm):
    for key in ("oleg", "land"):  # never pressed /start, and a landing lead with no Telegram at all
        r = await crm.post(f"/api/hr/candidates/{crm.ids[key]}/messages", headers=as_("hr"), json={"template": "offer", "text": "Привет"})
        assert_error(r, 409, "bot_not_started")
        assert r.json()["error"]["code"] == "bot_not_started"
        assert await events(crm, key, "message") == []
    assert await outbox_messages(crm) == []
    await crm.db.execute("UPDATE users SET bot_started_at = now() WHERE id = $1", crm.users["oleg"])
    assert (await crm.post(f"/api/hr/candidates/{crm.ids['oleg']}/messages", headers=as_("hr"), json={"template": "offer"})).status_code == 202
    d = (await get(crm, "hr", f"/api/hr/candidates/{crm.ids['oleg']}")).json()
    assert d["canMessage"] is True


async def test_message_roles_and_hidden_leads(crm):
    lid = crm.ids["ivan"]
    body = {"template": "offer", "text": "x"}
    assert_error(await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("analyst"), json=body), 403, "forbidden")
    assert (await crm.post(f"/api/hr/candidates/{lid}/messages", json=body)).status_code == 401
    for key in ("revoked", "bot", "test"):
        assert_error(await crm.post(f"/api/hr/candidates/{crm.ids[key]}/messages", headers=as_("hr"), json=body), 404, "lead_not_found")
    assert_error(await crm.post("/api/hr/candidates/999999/messages", headers=as_("hr"), json=body), 404, "lead_not_found")
    assert await outbox_messages(crm) == []


# ---------------------------------------------------------------- pipeline

STATUS_ORDER = ["new", "screening", "interview", "offer", "hired", "rejected", "reserve"]


async def test_pipeline_columns(crm):
    r = await get(crm, "hr", "/api/hr/pipeline")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    body = r.json()
    assert set(body) == {"columns"} and [c["status"] for c in body["columns"]] == STATUS_ORDER
    cols = {c["status"]: c for c in body["columns"]}
    assert {s: c["count"] for s, c in cols.items()} == {"new": 2, "screening": 1, "interview": 1, "offer": 1, "hired": 1, "rejected": 1, "reserve": 1}
    assert all(set(c) == {"status", "count", "items"} and len(c["items"]) == c["count"] for c in cols.values())
    assert all(set(i) == ROW_KEYS for c in cols.values() for i in c["items"])
    assert sorted(i["id"] for i in cols["new"]["items"]) == sorted(ids(crm, "ivan", "land"))
    # a column lists the most recently moved first
    await patch(crm, "hr", "land", {"status": "interview", "version": 1})
    await patch(crm, "hr", "ivan", {"status": "interview", "version": 1})
    cols = {c["status"]: c for c in (await get(crm, "hr", "/api/hr/pipeline")).json()["columns"]}
    assert [i["id"] for i in cols["interview"]["items"]] == ids(crm, "ivan", "land", "oleg") and cols["interview"]["count"] == 3
    assert cols["new"]["count"] == 0 and cols["new"]["items"] == []


async def test_pipeline_filters_cap_and_masking(crm):
    cols = {c["status"]: c for c in (await get(crm, "hr", "/api/hr/pipeline", profession="lab")).json()["columns"]}
    assert {s: c["count"] for s, c in cols.items() if c["count"]} == {"new": 1, "screening": 1}  # land (vacancy), anna (vacancy + fit)
    cols = {c["status"]: c for c in (await get(crm, "hr", "/api/hr/pipeline", assignee="me")).json()["columns"]}
    assert {s: c["count"] for s, c in cols.items() if c["count"]} == {"screening": 1}
    cols = {c["status"]: c for c in (await get(crm, "hr", "/api/hr/pipeline", assignee="none")).json()["columns"]}
    assert sum(c["count"] for c in cols.values()) == len(VISIBLE) - 1
    assert_error(await get(crm, "hr", "/api/hr/pipeline", profession="pilot"), 422, "validation_error")
    assert_error(await get(crm, "hr", "/api/hr/pipeline", assignee="you"), 422, "validation_error")
    # the column carries at most 50 cards but counts all of them
    await crm.db.execute(
        "INSERT INTO leads (season_id, full_name, university, consent, consent_at, consent_text_version, status, created_at, updated_at, "
        "stage_changed_at) SELECT $1, 'Массовый ' || g, 'U', true, now(), 'v', 'offer', now(), now(), $2::timestamptz + interval '1 day' + g * interval '1 minute' "
        "FROM generate_series(1, 60) g", crm.season, BASE)
    col = next(c for c in (await get(crm, "hr", "/api/hr/pipeline")).json()["columns"] if c["status"] == "offer")
    assert col["count"] == 61 and len(col["items"]) == 50
    stamps = [i["stageChangedAt"] for i in col["items"]]
    assert stamps == sorted(stamps, reverse=True) and col["items"][0]["fullName"] == "Массовый 60"
    # the analyst gets masked cards
    masked = next(c for c in (await get(crm, "analyst", "/api/hr/pipeline")).json()["columns"] if c["status"] == "offer")
    assert masked["count"] == 61 and all(i["piiMasked"] and i["fullName"] == f"Кандидат #{i['id']}" for i in masked["items"])


# ---------------------------------------------------------------- landing intake without a game account

async def test_landing_lead_becomes_a_candidate(crm):
    data = {"fullName": "Сергеев Павел", "birthDate": (datetime.now().date().replace(year=datetime.now().year - 22)).isoformat(),
            "phone": "8 (999) 555-44-33", "university": "Колледж пищевой промышленности", "roleId": "lab", "department": "lab",
            "practiceType": "paid", "readiness": "later", "source": "land_test", "consent": True, "consentTextVersion": "test-v1",
            "submissionId": str(uuid4()), "receiptToken": str(uuid4())}
    r = await crm.post("/api/leads/public", json=data)
    assert r.status_code == 201, r.text
    lid = r.json()["id"]
    assert await crm.db.fetchval("SELECT user_id FROM leads WHERE id = $1", lid) is None
    rows = (await get(crm, "hr", "/api/hr/candidates", q="Сергеев")).json()
    assert rows["total"] == 1
    row = rows["items"][0]
    assert row["id"] == lid and row["source"] == "land_test" and row["status"] == "new" and row["roleId"] == "lab"
    assert row["university"] == "Колледж пищевой промышленности" and row["seasonRank"] is None and row["achievementPoints"] == 0
    d = (await get(crm, "hr", f"/api/hr/candidates/{lid}")).json()
    assert d["phone"] == "+79995554433" and d["canMessage"] is False and d["game"]["level"] == 0
    # HR works with it like with any other candidate
    v = d["version"]
    r = await crm.patch(f"/api/hr/candidates/{lid}", headers=as_("hr"), json={"status": "screening", "version": v})
    assert r.status_code == 200 and r.json()["status"] == "screening"
    assert (await crm.post(f"/api/hr/candidates/{lid}/notes", headers=as_("hr"), json={"text": "Позвонить"})).status_code == 201
    assert_error(await crm.post(f"/api/hr/candidates/{lid}/messages", headers=as_("hr"), json={"template": "offer"}), 409, "bot_not_started")
    pipe = {c["status"]: c for c in (await get(crm, "analyst", "/api/hr/pipeline")).json()["columns"]}
    assert any(i["id"] == lid and i["fullName"] == f"Кандидат #{lid}" for i in pipe["screening"]["items"])
    # revoking the consent hides the candidate everywhere
    rr = await crm.delete(f"/api/leads/public/{data['submissionId']}", params={"token": data["receiptToken"]}) \
        if False else None
    await crm.db.execute("UPDATE leads SET consent_revoked_at = now() WHERE id = $1", lid)
    assert_error(await get(crm, "hr", f"/api/hr/candidates/{lid}"), 404, "lead_not_found")
    assert lid not in await page_ids(crm)


async def test_revoking_consent_removes_a_game_candidate(crm):
    assert crm.ids["maria"] in await page_ids(crm)
    await crm.db.execute("UPDATE leads SET consent_revoked_at = now() WHERE id = $1", crm.ids["maria"])
    assert crm.ids["maria"] not in await page_ids(crm)
    cols = {c["status"]: c for c in (await get(crm, "hr", "/api/hr/pipeline")).json()["columns"]}
    assert cols["offer"]["count"] == 0
    assert (await get(crm, "hr", "/api/hr/candidates")).json()["total"] == len(VISIBLE) - 1
