"""C5: vacancies (CRUD, roles, validation, matching by `vacancy_fit`). Real Postgres + Redis (skipped when absent)."""
from __future__ import annotations

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.hr import vacancies as vac
from tests.helpers import TEST_DB_URL, dev
from tests.test_c3_hr_access import STRANGER, TG, as_, hr  # noqa: F401  (the `hr` fixture: one HR user per role)
from tests.test_errors import assert_error

URL = "/api/hr/vacancies"
REQ = {"competencies": {"speed": 60}, "achievements": ["first_line"]}  # two requirements: 50 points each


def body(**kw):
    return {"professionId": "operator", "title": "Оператор линии", "city": "Москва", "active": True, "requirements": REQ, **kw}


# ---------------------------------------------------------------- seeding

async def season(db) -> int:
    return await db.fetchval("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7) RETURNING id")


async def candidate(db, season_id: int, tg: int, name: str, *, speed: int | None = None, earned=(), points: int = 0,
                    bot=False, test=False, revoked=False, landing=False) -> int:
    """A lead (+ player, score row, earned achievements). Returns the lead id."""
    uid = None if landing else await db.fetchval(
        "INSERT INTO users (tg_id, first_name, is_bot, is_test) VALUES ($1, $2, $3, $4) RETURNING id", tg, name, bot, test)
    lead = await db.fetchval(
        "INSERT INTO leads (user_id, season_id, full_name, university, consent, consent_at, consent_text_version, consent_revoked_at, phone) "
        "VALUES ($1, $2, $3, 'МГУПП', true, now(), 'v1', CASE WHEN $4 THEN now() END, '+79990000000') RETURNING id",
        uid, season_id, name, revoked)
    if uid is not None:
        await db.execute("INSERT INTO factories (user_id, season_id, name, state) VALUES ($1, $2, 'F', '{}'::jsonb)", uid, season_id)
        if speed is not None:
            await db.execute("INSERT INTO player_scores (user_id, season_id, achievement_points, competency) VALUES ($1, $2, $3, $4::jsonb)",
                             uid, season_id, points, '{"speed": %d}' % speed)
        for a in earned:
            await db.execute("INSERT INTO achievements_earned (user_id, achievement_id, tier, points, catalog_version, granted_via) "
                             "VALUES ($1, $2, 'bronze', 10, 1, 'hook')", uid, a)
    return lead


@pytest_asyncio.fixture
async def pool(hr):
    """A fixed set of candidates for the requirement REQ (speed >= 60 and `first_line`)."""
    s = await season(hr.db)
    hr.season = s
    hr.lead = {
        "full": await candidate(hr.db, s, 7001, "Full Match", speed=80, earned=["first_line"], points=200),   # 100
        "half": await candidate(hr.db, s, 7002, "Half Match", speed=30, earned=["first_line"], points=80),    # 25 + 50 = 75
        "low": await candidate(hr.db, s, 7003, "Low Match", speed=30, points=10),                             # 25
        "ach": await candidate(hr.db, s, 7004, "Ach Only", earned=["first_line"]),                            # 50: no competency row
        "bot": await candidate(hr.db, s, 7005, "Bot", speed=99, earned=["first_line"], bot=True),
        "test": await candidate(hr.db, s, 7006, "Test", speed=99, earned=["first_line"], test=True),
        "revoked": await candidate(hr.db, s, 7007, "Revoked", speed=99, earned=["first_line"], revoked=True),
        "landing": await candidate(hr.db, s, 0, "Landing", landing=True),
    }
    yield hr
    await hr.db.execute("TRUNCATE leads, seasons, users RESTART IDENTITY CASCADE")  # a landing lead would break test_db's downgrade


@pytest_asyncio.fixture
async def sm(hr):
    engine = create_async_engine(TEST_DB_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def create(hr, role="hr_lead", **kw):
    r = await hr.post(URL, json=body(**kw), headers=as_(role))
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------- schema

async def test_migration_creates_vacancies(hr):
    cols = {r["column_name"]: r for r in await hr.db.fetch(
        "SELECT column_name, is_nullable FROM information_schema.columns WHERE table_name = 'vacancies'")}
    assert set(cols) == {"id", "profession_id", "title", "city", "active", "requirements", "created_by", "created_at", "updated_at"}
    assert await hr.db.fetchval("SELECT version_num FROM alembic_version") >= "0017"


# ---------------------------------------------------------------- roles

async def test_anonymous_and_stranger(hr):
    assert_error(await hr.get(URL), 401, "unauthorized")
    assert_error(await hr.post(URL, json=body()), 401, "unauthorized")
    assert_error(await hr.get(URL, headers=dev(STRANGER)), 403, "forbidden")
    assert_error(await hr.post(URL, json=body(), headers=dev(STRANGER)), 403, "forbidden")


@pytest.mark.parametrize("role", ["analyst", "hr", "hr_lead", "admin"])
async def test_every_role_reads(hr, role):
    r = await hr.get(URL, headers=as_(role))
    assert r.status_code == 200 and r.json() == {"items": []}
    assert r.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("role", ["analyst", "hr"])
async def test_analyst_and_hr_cannot_write(hr, role):
    vid = (await create(hr))["id"]
    assert_error(await hr.post(URL, json=body(), headers=as_(role)), 403, "forbidden")
    assert_error(await hr.patch(f"{URL}/{vid}", json={"title": "x"}, headers=as_(role)), 403, "forbidden")
    assert await hr.db.fetchval("SELECT count(*) FROM vacancies") == 1
    assert await hr.db.fetchval("SELECT title FROM vacancies WHERE id = $1", vid) == "Оператор линии"


@pytest.mark.parametrize("role", ["hr_lead", "admin"])
async def test_lead_and_admin_write(hr, role):
    v = await create(hr, role)
    r = await hr.patch(f"{URL}/{v['id']}", json={"active": False}, headers=as_(role))
    assert r.status_code == 200 and r.json()["active"] is False


# ---------------------------------------------------------------- create / read shape

async def test_create_shape_and_defaults(hr):
    v = await create(hr)
    assert v == {"id": v["id"], "professionId": "operator", "title": "Оператор линии", "city": "Москва", "active": True,
                 "requirements": REQ, "matching": 0}
    row = await hr.db.fetchrow("SELECT * FROM vacancies WHERE id = $1", v["id"])
    assert row["created_by"] == hr.ids["hr_lead"] and row["created_at"] and row["updated_at"]
    # only professionId and title are required
    r = await hr.post(URL, json={"professionId": "lab", "title": "  Лаборант  "}, headers=as_("admin"))
    assert r.status_code == 201
    assert r.json() == {"id": r.json()["id"], "professionId": "lab", "title": "Лаборант", "city": "", "active": True,
                        "requirements": {"competencies": {}, "achievements": []}, "matching": 0}
    items = (await hr.get(URL, headers=as_("analyst"))).json()["items"]
    assert [i["title"] for i in items] == ["Оператор линии", "Лаборант"]  # by id


async def test_requirements_are_normalised(hr):
    v = await create(hr, requirements={"competencies": {"speed": 0, "company": 100}, "achievements": ["first_line", "first_line", "quiz_8"]})
    assert v["requirements"] == {"competencies": {"speed": 0, "company": 100}, "achievements": ["first_line", "quiz_8"]}


@pytest.mark.parametrize("patch", [
    {"professionId": "pilot"}, {"professionId": ""}, {"professionId": None, "title": None}, {"title": ""}, {"title": "   "},
    {"title": "x" * 161}, {"city": "x" * 81}, {"active": "maybe"},
    {"requirements": {"competencies": {"charisma": 50}}}, {"requirements": {"competencies": {"speed": 101}}},
    {"requirements": {"competencies": {"speed": -1}}}, {"requirements": {"competencies": {"speed": "60"}}},
    {"requirements": {"competencies": {"speed": 60.5}}}, {"requirements": {"competencies": {"speed": True}}},
    {"requirements": {"achievements": ["no_such_achievement"]}}, {"requirements": {"achievements": "first_line"}},
    {"requirements": {"achievements": ["first_line"] * 61}}, {"requirements": "all"}, {"requirements": {"competencies": []}},
])
async def test_validation_rejects(hr, patch):
    r = await hr.post(URL, json={**body(), **patch}, headers=as_("hr_lead"))
    assert_error(r, 422, "validation_error")
    assert await hr.db.fetchval("SELECT count(*) FROM vacancies") == 0


async def test_create_requires_profession_and_title(hr):
    assert_error(await hr.post(URL, json={"title": "x"}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.post(URL, json={"professionId": "lab"}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.post(URL, content=b"not json", headers={**as_("hr_lead"), "Content-Type": "application/json"}), 422, "validation_error")


# ---------------------------------------------------------------- patch

async def test_patch_updates_only_what_is_sent(hr):
    v = await create(hr)
    r = await hr.patch(f"{URL}/{v['id']}", json={"title": "Старший оператор", "active": False}, headers=as_("hr_lead"))
    assert r.status_code == 200
    assert r.json() == {**v, "title": "Старший оператор", "active": False}
    before = await hr.db.fetchrow("SELECT created_at, updated_at, created_by FROM vacancies WHERE id = $1", v["id"])
    r = await hr.patch(f"{URL}/{v['id']}", json={"requirements": {"competencies": {"accuracy": 70}, "achievements": []},
                                                  "professionId": "lab", "city": "Мытищи"}, headers=as_("admin"))
    assert r.json()["requirements"] == {"competencies": {"accuracy": 70}, "achievements": []}  # replaced as a whole
    assert (r.json()["professionId"], r.json()["city"], r.json()["title"]) == ("lab", "Мытищи", "Старший оператор")
    after = await hr.db.fetchrow("SELECT created_at, updated_at, created_by FROM vacancies WHERE id = $1", v["id"])
    assert after["created_at"] == before["created_at"] and after["created_by"] == before["created_by"]
    assert after["updated_at"] > before["updated_at"]


async def test_patch_errors(hr):
    v = await create(hr)
    path = f"{URL}/{v['id']}"
    assert_error(await hr.patch(f"{URL}/99999", json={"title": "x"}, headers=as_("hr_lead")), 404, "vacancy_not_found")
    assert_error(await hr.patch(f"{URL}/0", json={"title": "x"}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.patch(f"{URL}/abc", json={"title": "x"}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.patch(path, json={}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.patch(path, json={"title": None}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.patch(path, json={"professionId": "pilot"}, headers=as_("hr_lead")), 422, "validation_error")
    assert_error(await hr.patch(path, json={"requirements": {"competencies": {"x": 1}}}, headers=as_("hr_lead")), 422, "validation_error")
    assert (await hr.get(URL, headers=as_("hr"))).json()["items"] == [v]  # nothing changed
    assert_error(await hr.patch(path, json={"title": "x"}), 401, "unauthorized")


# ---------------------------------------------------------------- matching

async def test_matching_counts_candidates_with_fit_70(pool):
    v = await create(pool)
    assert v["matching"] == 2  # full (100) and half (75); low 25, ach-only 50; bot / test / revoked / landing never
    items = (await pool.get(URL, headers=as_("analyst"))).json()["items"]
    assert [i["matching"] for i in items] == [2]
    easy = await create(pool, title="Easy", requirements={"competencies": {"speed": 20}, "achievements": []})
    assert easy["matching"] == 3  # speed 80, 30, 30 all reach 20; "ach" has no competency row, bot / test / revoked are out
    empty = await create(pool, title="No requirements", requirements={})
    assert empty["matching"] == 0  # no requirements = fit 0
    patched = await pool.patch(f"{URL}/{v['id']}", json={"requirements": {"competencies": {}, "achievements": ["first_line"]}}, headers=as_("hr_lead"))
    assert patched.json()["matching"] == 3  # first_line: full, half, ach-only (the bot / test / revoked holders do not count)


async def test_matching_uses_the_score_row_of_the_factory_season(pool):
    only_speed = {"competencies": {"speed": 60}, "achievements": []}
    v = await create(pool, requirements=only_speed)
    assert v["matching"] == 1  # full (80); half and low have 30, ach-only has no score row
    uid = await pool.db.fetchval("SELECT user_id FROM leads WHERE id = $1", pool.lead["low"])
    s2 = await pool.db.fetchval("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7) RETURNING id")
    await pool.db.execute("INSERT INTO player_scores (user_id, season_id, competency) VALUES ($1, $2, '{\"speed\": 90}'::jsonb)", uid, s2)
    assert (await pool.get(URL, headers=as_("hr"))).json()["items"][0]["matching"] == 1  # the factory is still in the first season
    await pool.db.execute("UPDATE factories SET season_id = $1 WHERE user_id = $2", s2, uid)
    assert (await pool.get(URL, headers=as_("hr"))).json()["items"][0]["matching"] == 2


async def test_top_candidates_endpoint_and_masking(pool):
    v = await create(pool)
    r = await pool.get(f"{URL}/{v['id']}/candidates", headers=as_("hr"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [(i["id"], i["score"]) for i in items] == [(pool.lead["full"], 100), (pool.lead["half"], 75), (pool.lead["ach"], 50), (pool.lead["low"], 25)]
    assert items[0]["fullName"] == "Full Match" and items[0]["piiMasked"] is False and items[0]["achievementPoints"] == 200
    assert all(i["reasons"] and {"text", "delta"} <= set(i["reasons"][0]) for i in items)
    assert sum(r["delta"] for r in items[1]["reasons"]) == 75
    masked = (await pool.get(f"{URL}/{v['id']}/candidates?limit=2", headers=as_("analyst"))).json()["items"]
    assert [i["fullName"] for i in masked] == [f"Кандидат #{pool.lead['full']}", f"Кандидат #{pool.lead['half']}"]
    assert all(i["piiMasked"] is True for i in masked)
    assert_error(await pool.get(f"{URL}/{v['id']}/candidates?limit=0", headers=as_("hr")), 422, "validation_error")
    assert_error(await pool.get(f"{URL}/999/candidates", headers=as_("hr")), 404, "vacancy_not_found")
    assert_error(await pool.get(f"{URL}/{v['id']}/candidates", headers=dev(STRANGER)), 403, "forbidden")


async def test_fits_for_user_helper(pool, sm):
    v1 = await create(pool)
    await create(pool, title="Inactive", active=False)
    uid = await pool.db.fetchval("SELECT user_id FROM leads WHERE id = $1", pool.lead["half"])
    async with sm() as session:
        fits = await vac.fits_for_user(session, uid)
        all_fits = await vac.fits_for_user(session, uid, only_active=False)
        nobody = await vac.fits_for_user(session, 10**9)
    assert [(f["vacancyId"], f["score"], f["professionId"]) for f in fits] == [(v1["id"], 75, "operator")]
    assert len(all_fits) == 2 and nobody == []
    assert sum(r["delta"] for r in fits[0]["reasons"]) == 75


def test_validation_models_are_strict_about_the_catalog():
    assert vac.RequirementsIn(competencies={"speed": 5}, achievements=["first_line", "first_line"]).dump() == {
        "competencies": {"speed": 5}, "achievements": ["first_line"]}
    assert vac.MATCH_MIN == 70
