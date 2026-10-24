"""C3: HR roles and access (hr_users, lead_events, GET /api/hr/me, /api/hr/users, no-store, rate limit `hr`, PII masking).
Real Postgres + Redis (skipped when absent)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import asyncpg
import pytest
import pytest_asyncio
from fastapi import Depends
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.errors import AppError
from app.hr import access
from app.hr.access import (EVENT_KINDS, HrActor, NoStoreMiddleware, ensure_admin_remains, hr_actor, log_event, mask_pii,
                           mask_pii_many, record_event, seed_admins)
from app.ratelimit import LIMITS
from app.settings import Settings
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, dev, tma
from tests.test_errors import assert_error
from tests.test_s14_web_auth import bearer, code_login

ROLES = ["hr", "hr_lead", "analyst", "admin"]
TG = {"hr": 8101, "hr_lead": 8102, "analyst": 8103, "admin": 8104}
STRANGER = 8199  # authenticated, never an HR user
NO_STORE = "no-store"


@pytest_asyncio.fixture
async def hr(make_client, clean_db, clean_redis, fake_sim):
    """Client on the throwaway Postgres + Redis, limiter off. One HR user per role (ids in TG), row ids in `.ids`."""
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_enabled=False)
    c.db, c.redis = clean_db, clean_redis
    c.ids = {}
    for role in ROLES:
        c.ids[role] = await add_hr(c.db, TG[role], role)
    c.app.add_api_route("/t/hr/analyst", _echo("analyst"), methods=["GET"])
    c.app.add_api_route("/t/hr/hr", _echo("hr"), methods=["GET"])
    c.app.add_api_route("/t/hr/hr_lead", _echo("hr_lead"), methods=["GET"])
    c.app.add_api_route("/t/hr/admin", _echo("admin"), methods=["GET"])
    return c


def _echo(min_role: str):
    async def route(actor: HrActor = Depends(hr_actor(min_role))):
        return {"role": actor.role, "id": actor.id}
    return route


async def add_hr(db, tg: int, role: str, *, active: bool = True, created_by: int | None = None) -> int:
    return await db.fetchval("INSERT INTO hr_users (tg_id, role, active, created_by) VALUES ($1, $2, $3, $4) RETURNING id",
                             tg, role, active, created_by)


def as_(role: str, name: str | None = None) -> dict[str, str]:
    return dev(TG[role], name or role)


# ---------------------------------------------------------------- schema, settings, limits

async def test_migration_creates_hr_tables(hr):
    names = {r["tablename"] for r in await hr.db.fetch("SELECT tablename FROM pg_tables WHERE schemaname = 'public'")}
    assert {"hr_users", "lead_events"} <= names
    assert await hr.db.fetchval("SELECT version_num FROM alembic_version") >= "0013"
    idx = await hr.db.fetchval("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_lead_events_lead_created'")
    assert "(lead_id, created_at)" in idx


async def test_hr_users_constraints(hr):
    with pytest.raises(asyncpg.CheckViolationError):
        await add_hr(hr.db, 8300, "superuser")
    with pytest.raises(asyncpg.UniqueViolationError):
        await add_hr(hr.db, TG["hr"], "hr")
    row = await hr.db.fetchrow("SELECT * FROM hr_users WHERE id = $1", hr.ids["hr"])
    assert row["active"] is True and row["user_id"] is None and row["created_by"] is None and row["created_at"]


def test_settings_parse_hr_admin_ids():
    s = Settings(hr_admin_tg_ids="10, 20;x 30 -4 0 20 99999999999999999999")
    assert s.hr_admin_ids == [10, 20, 30]
    assert Settings().hr_admin_ids == []


def test_hr_rate_limit_is_declared():
    assert LIMITS["hr"].limit == 120 and LIMITS["hr"].window_s == 60


def test_unknown_role_fails_at_declaration():
    with pytest.raises(KeyError):
        hr_actor("superuser")


def test_role_order_and_permissions():
    assert [access.at_least(r, "hr") for r in ("analyst", "hr", "hr_lead", "admin")] == [False, True, True, True]
    assert [access.at_least(r, "hr_lead") for r in ("analyst", "hr", "hr_lead", "admin")] == [False, False, True, True]
    assert [access.at_least(r, "admin") for r in ("analyst", "hr", "hr_lead", "admin")] == [False, False, False, True]
    assert access.at_least("analyst", "analyst") and not access.at_least("nobody", "analyst")
    assert access.permissions_of("analyst") == ["candidates.read", "analytics.read", "vacancies.read"]
    assert "export" not in access.permissions_of("hr") and "export" in access.permissions_of("hr_lead")
    assert "users.manage" in access.permissions_of("admin") and "users.manage" not in access.permissions_of("hr_lead")
    assert set(access.ROLES) == {"hr", "hr_lead", "analyst", "admin"}
    for role in access.ROLES:  # every role only holds known permissions; admin holds all of them
        assert set(access.permissions_of(role)) <= access.PERMISSIONS
    assert set(access.permissions_of("admin")) == access.PERMISSIONS


# ---------------------------------------------------------------- seed from HR_ADMIN_TG_IDS

async def test_startup_seeds_env_ids_as_admins_idempotently(make_client, clean_db, clean_redis):
    u = await clean_db.fetchval("INSERT INTO users (tg_id, first_name) VALUES (601, 'Known') RETURNING id")
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, hr_admin_tg_ids="601, 602;601")
    rows = await clean_db.fetch("SELECT * FROM hr_users ORDER BY tg_id")
    assert [(r["tg_id"], r["role"], r["active"], r["created_by"]) for r in rows] == [(601, "admin", True, None),
                                                                                       (602, "admin", True, None)]
    assert rows[0]["user_id"] == u and rows[1]["user_id"] is None  # linked when the player row exists
    # an admin changed things meanwhile: a restart (second app) and a direct re-seed never touch existing rows
    await clean_db.execute("UPDATE hr_users SET role = 'analyst', active = false WHERE tg_id = 602")
    await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, hr_admin_tg_ids="601,602,603")
    sm = c.app.state.sessionmaker
    assert await seed_admins(sm, [601, 602, 603]) == []
    rows = {r["tg_id"]: r for r in await clean_db.fetch("SELECT * FROM hr_users")}
    assert set(rows) == {601, 602, 603}
    assert (rows[602]["role"], rows[602]["active"]) == ("analyst", False) and rows[603]["role"] == "admin"


async def test_seeded_admin_can_use_the_crm_and_gets_linked(make_client, clean_db, clean_redis):
    c = await make_client(database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, rate_limit_enabled=False,
                          hr_admin_tg_ids="777")
    r = await c.get("/api/hr/me", headers=dev(777, "Boss"))
    assert r.status_code == 200 and r.json()["role"] == "admin" and r.json()["name"] == "Boss"
    row = await clean_db.fetchrow("SELECT h.user_id, u.id AS uid FROM hr_users h JOIN users u ON u.tg_id = h.tg_id")
    assert row["user_id"] == row["uid"]  # linked on the first request


async def test_seed_failure_never_breaks_the_startup(make_client):
    c = await make_client(hr_admin_tg_ids="5")  # unreachable database: the lifespan only logs
    assert (await c.get("/health")).status_code == 200


# ---------------------------------------------------------------- 401 vs 403 and the role matrix

async def test_anonymous_is_401_everywhere(hr):
    for method, path in [("GET", "/api/hr/me"), ("GET", "/api/hr/users"), ("POST", "/api/hr/users"),
                         ("PATCH", "/api/hr/users/1"), ("GET", "/t/hr/analyst"), ("GET", "/t/hr/admin")]:
        r = await hr.request(method, path, json={} if method != "GET" else None)
        assert_error(r, 401, "unauthorized")
    assert_error(await hr.get("/api/hr/me", headers={"Authorization": "Bearer " + "x" * 43}), 401, "unauthorized")
    assert_error(await hr.get("/api/hr/me", headers={"Authorization": "Basic abc"}), 401, "unauthorized")


async def test_non_hr_and_inactive_are_403(hr):
    assert_error(await hr.get("/api/hr/me", headers=dev(STRANGER, "Nobody")), 403, "forbidden")
    await hr.db.execute("UPDATE hr_users SET active = false WHERE tg_id = $1", TG["admin"])
    assert_error(await hr.get("/api/hr/me", headers=as_("admin")), 403, "forbidden")
    assert_error(await hr.get("/api/hr/users", headers=as_("admin")), 403, "forbidden")
    # a deactivated person is one request away from losing access: others are unaffected
    assert (await hr.get("/api/hr/me", headers=as_("hr"))).status_code == 200


MATRIX = {  # route -> role -> status (analyst is the read-only floor: below `hr`)
    "/t/hr/analyst": {"hr": 200, "hr_lead": 200, "analyst": 200, "admin": 200},
    "/t/hr/hr": {"hr": 200, "hr_lead": 200, "analyst": 403, "admin": 200},
    "/t/hr/hr_lead": {"hr": 403, "hr_lead": 200, "analyst": 403, "admin": 200},
    "/t/hr/admin": {"hr": 403, "hr_lead": 403, "analyst": 403, "admin": 200},
    "/api/hr/me": {"hr": 200, "hr_lead": 200, "analyst": 200, "admin": 200},
    "/api/hr/users": {"hr": 403, "hr_lead": 403, "analyst": 403, "admin": 200},
}


@pytest.mark.parametrize("path", list(MATRIX))
async def test_role_matrix_for_get_routes(hr, path):
    for role in ROLES:
        r = await hr.get(path, headers=as_(role))
        assert r.status_code == MATRIX[path][role], (path, role, r.text)
        if r.status_code == 403:
            assert_error(r, 403, "forbidden")
    assert_error(await hr.get(path, headers=dev(STRANGER)), 403, "forbidden")
    assert_error(await hr.get(path), 401, "unauthorized")


async def test_role_matrix_for_writes(hr):
    for role in ROLES:
        r = await hr.post("/api/hr/users", json={"tgId": 9000 + len(role), "role": "hr"}, headers=as_(role))
        assert r.status_code == (201 if role == "admin" else 403), (role, r.text)
        r = await hr.patch(f"/api/hr/users/{hr.ids['hr']}", json={"role": "hr"}, headers=as_(role))
        assert r.status_code == (200 if role == "admin" else 403), (role, r.text)
    # the 403s did not write anything: only the admin's single POST created a row
    assert await hr.db.fetchval("SELECT count(*) FROM hr_users") == 4 + 1
    assert_error(await hr.post("/api/hr/users", json={"tgId": 1, "role": "hr"}, headers=dev(STRANGER)), 403, "forbidden")
    assert_error(await hr.patch("/api/hr/users/1", json={"role": "hr"}, headers=dev(STRANGER)), 403, "forbidden")


async def test_bearer_web_session_is_the_main_path(hr):
    s = await code_login(hr, TG["hr_lead"], first_name="Lena", last_name="Lead")
    r = await hr.get("/api/hr/me", headers=bearer(s["token"]))
    assert r.status_code == 200
    assert r.json() == {"tgId": TG["hr_lead"], "name": "Lena Lead", "role": "hr_lead",
                        "permissions": access.permissions_of("hr_lead")}
    assert (await hr.get("/api/hr/users", headers=bearer(s["token"]))).status_code == 403
    # a player's session without an hr_users row: authenticated but forbidden
    other = await code_login(hr, STRANGER, first_name="Player")
    assert_error(await hr.get("/api/hr/me", headers=bearer(other["token"])), 403, "forbidden")
    # logout / revoked session: 401 again, not 403
    assert (await hr.delete("/api/auth/web/session", headers=bearer(s["token"]))).status_code == 204
    assert_error(await hr.get("/api/hr/me", headers=bearer(s["token"])), 401, "unauthorized")


async def test_deactivation_takes_effect_on_a_live_session(hr):
    s = await code_login(hr, TG["hr"])
    assert (await hr.get("/api/hr/me", headers=bearer(s["token"]))).status_code == 200
    await hr.db.execute("UPDATE hr_users SET active = false WHERE tg_id = $1", TG["hr"])
    assert_error(await hr.get("/api/hr/me", headers=bearer(s["token"])), 403, "forbidden")


async def test_telegram_init_data_works_too(hr):
    r = await hr.get("/api/hr/me", headers=tma({"id": TG["analyst"], "first_name": "Ann"}))
    assert r.status_code == 200 and r.json()["role"] == "analyst" and r.json()["name"] == "Ann"
    assert_error(await hr.get("/api/hr/me", headers=tma({"id": STRANGER, "first_name": "X"})), 403, "forbidden")


# ---------------------------------------------------------------- GET /api/hr/me

@pytest.mark.parametrize("role", ROLES)
async def test_me_shape_and_permissions(hr, role):
    r = await hr.get("/api/hr/me", headers=as_(role, "Ivan Petrov"))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"tgId", "name", "role", "permissions"}
    assert body["tgId"] == TG[role] and body["role"] == role and body["name"] == "Ivan Petrov"
    assert body["permissions"] == access.permissions_of(role)


async def test_permission_matrix_of_the_plan(hr):
    got = {}
    for role in ROLES:
        got[role] = (await hr.get("/api/hr/me", headers=as_(role))).json()["permissions"]
    assert "candidates.pii" not in got["analyst"] and "candidates.write" not in got["analyst"]
    assert {"candidates.pii", "candidates.write"} <= set(got["hr"]) and "export" not in got["hr"]
    assert {"export", "vacancies.write"} <= set(got["hr_lead"]) and "users.manage" not in got["hr_lead"]
    assert "users.manage" in got["admin"]


# ---------------------------------------------------------------- /api/hr/users

def user_keys(u: dict) -> set[str]:
    return set(u)


async def test_list_users(hr):
    await hr.get("/api/hr/me", headers=as_("hr", "Hanna"))  # creates the player row and links it
    r = await hr.get("/api/hr/users", headers=as_("admin"))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["id"] for i in items] == sorted(i["id"] for i in items) and len(items) == 4
    assert user_keys(items[0]) == {"id", "tgId", "userId", "name", "username", "role", "active", "createdAt", "createdBy"}
    by_tg = {i["tgId"]: i for i in items}
    assert by_tg[TG["hr"]]["name"] == "Hanna" and by_tg[TG["hr"]]["userId"] is not None
    assert by_tg[TG["analyst"]]["name"] is None and by_tg[TG["analyst"]]["userId"] is None  # never seen as a player
    assert by_tg[TG["admin"]]["createdBy"] is None and by_tg[TG["admin"]]["createdAt"].endswith("Z")


async def test_add_user(hr):
    await hr.db.execute("INSERT INTO users (tg_id, first_name, username) VALUES (9100, 'Olga', 'olga')")
    r = await hr.post("/api/hr/users", json={"tgId": 9100, "role": "analyst"}, headers=as_("admin"))
    assert r.status_code == 201, r.text
    u = r.json()
    assert (u["tgId"], u["role"], u["active"], u["name"], u["username"]) == (9100, "analyst", True, "Olga", "olga")
    assert u["createdBy"] == hr.ids["admin"] and u["userId"] is not None
    # the new user can sign in right away with their role
    me = await hr.get("/api/hr/me", headers=dev(9100))
    assert me.status_code == 200 and me.json()["role"] == "analyst"
    # unknown as a player yet: no name, no link
    r = await hr.post("/api/hr/users", json={"tg_id": 9101, "role": "hr_lead"}, headers=as_("admin"))  # snake_case too
    assert r.status_code == 201 and r.json()["name"] is None and r.json()["userId"] is None


async def test_add_existing_user_is_409(hr):
    r = await hr.post("/api/hr/users", json={"tgId": TG["hr"], "role": "admin"}, headers=as_("admin"))
    err = assert_error(r, 409, "hr_user_exists")
    assert err["message"]
    assert await hr.db.fetchval("SELECT role FROM hr_users WHERE tg_id = $1", TG["hr"]) == "hr"  # untouched
    await hr.db.execute("UPDATE hr_users SET active = false WHERE tg_id = $1", TG["hr"])
    assert_error(await hr.post("/api/hr/users", json={"tgId": TG["hr"], "role": "hr"}, headers=as_("admin")), 409,
                 "hr_user_exists")  # a deactivated user is reactivated with PATCH


@pytest.mark.parametrize("body", [
    {}, {"tgId": 5}, {"role": "hr"}, {"tgId": 0, "role": "hr"}, {"tgId": -5, "role": "hr"}, {"tgId": 2**63, "role": "hr"},
    {"tgId": "abc", "role": "hr"}, {"tgId": 5, "role": "boss"}, {"tgId": 5, "role": None}, {"tgId": 5, "role": "ADMIN"},
])
async def test_add_user_validation(hr, body):
    assert_error(await hr.post("/api/hr/users", json=body, headers=as_("admin")), 422, "validation_error")
    assert await hr.db.fetchval("SELECT count(*) FROM hr_users") == 4


async def test_patch_role_and_active(hr):
    r = await hr.patch(f"/api/hr/users/{hr.ids['hr']}", json={"role": "hr_lead"}, headers=as_("admin"))
    assert r.status_code == 200 and r.json()["role"] == "hr_lead" and r.json()["active"] is True
    assert (await hr.get("/t/hr/hr_lead", headers=as_("hr"))).status_code == 200  # the new role applies at once
    r = await hr.patch(f"/api/hr/users/{hr.ids['hr']}", json={"active": False}, headers=as_("admin"))
    assert r.status_code == 200 and r.json()["active"] is False and r.json()["role"] == "hr_lead"
    assert_error(await hr.get("/api/hr/me", headers=as_("hr")), 403, "forbidden")
    r = await hr.patch(f"/api/hr/users/{hr.ids['hr']}", json={"active": True, "role": "analyst"}, headers=as_("admin"))
    assert r.status_code == 200 and (r.json()["active"], r.json()["role"]) == (True, "analyst")
    assert (await hr.get("/api/hr/me", headers=as_("hr"))).json()["role"] == "analyst"


async def test_patch_validation_and_not_found(hr):
    h = as_("admin")
    for body in ({}, {"role": "boss"}, {"active": "maybe"}, {"role": None, "active": None}):
        assert_error(await hr.patch(f"/api/hr/users/{hr.ids['hr']}", json=body, headers=h), 422, "validation_error")
    assert_error(await hr.patch("/api/hr/users/999999", json={"role": "hr"}, headers=h), 404, "hr_user_not_found")
    assert_error(await hr.patch("/api/hr/users/0", json={"role": "hr"}, headers=h), 422, "validation_error")
    assert_error(await hr.patch("/api/hr/users/abc", json={"role": "hr"}, headers=h), 422, "validation_error")
    assert_error(await hr.patch(f"/api/hr/users/{2**63}", json={"role": "hr"}, headers=h), 422, "validation_error")


async def test_nobody_changes_themselves(hr):
    me = hr.ids["admin"]
    h = as_("admin")
    assert_error(await hr.patch(f"/api/hr/users/{me}", json={"role": "hr"}, headers=h), 409, "hr_self_change")
    assert_error(await hr.patch(f"/api/hr/users/{me}", json={"active": False}, headers=h), 409, "hr_self_change")
    assert_error(await hr.patch(f"/api/hr/users/{me}", json={"role": "analyst", "active": False}, headers=h), 409,
                 "hr_self_change")
    assert tuple(await hr.db.fetchrow("SELECT role, active FROM hr_users WHERE id = $1", me)) == ("admin", True)
    # a no-op on yourself is harmless
    r = await hr.patch(f"/api/hr/users/{me}", json={"role": "admin", "active": True}, headers=h)
    assert r.status_code == 200 and r.json()["role"] == "admin"
    # even with a second admin around, the guard is about identity, not about how many admins exist
    other = await add_hr(hr.db, 8200, "admin")
    assert_error(await hr.patch(f"/api/hr/users/{me}", json={"role": "hr"}, headers=h), 409, "hr_self_change")
    assert (await hr.patch(f"/api/hr/users/{other}", json={"role": "hr"}, headers=h)).status_code == 200


async def test_at_least_one_admin_remains(hr):
    """Two admins: either may demote / deactivate the OTHER, but then the survivor is stuck as the sole admin (self-change guard)."""
    b = await add_hr(hr.db, 8201, "admin")
    r = await hr.patch(f"/api/hr/users/{b}", json={"active": False}, headers=as_("admin"))
    assert r.status_code == 200
    assert_error(await hr.get("/api/hr/users", headers=dev(8201)), 403, "forbidden")  # the deactivated admin is out
    assert_error(await hr.patch(f"/api/hr/users/{hr.ids['admin']}", json={"role": "hr"}, headers=as_("admin")), 409,
                 "hr_self_change")
    assert await hr.db.fetchval("SELECT count(*) FROM hr_users WHERE role = 'admin' AND active") == 1


def test_last_admin_guard_unit():
    a, b = SimpleNamespace(id=1), SimpleNamespace(id=2)
    with pytest.raises(AppError) as e:
        ensure_admin_remains([a], 1, "hr", True)  # the only admin demoted
    assert e.value.code == "hr_last_admin" and e.value.status == 409
    with pytest.raises(AppError):
        ensure_admin_remains([a], 1, "admin", False)  # ... or deactivated
    with pytest.raises(AppError):
        ensure_admin_remains([], 7, "hr", True)  # nothing left at all
    ensure_admin_remains([a], 1, "admin", True)  # unchanged
    ensure_admin_remains([a, b], 1, "hr", True)  # another admin stays
    ensure_admin_remains([a], 2, "admin", True)  # a new admin appears
    ensure_admin_remains([a], 2, "hr", False)  # somebody else is touched


async def test_admins_demoting_each_other_at_once_cannot_empty_the_admin_group(hr):
    """The race the row locks are for: A demotes B while B demotes A. At most one wins, an active admin always remains."""
    b = await add_hr(hr.db, 8202, "admin")
    a = hr.ids["admin"]
    for _ in range(3):
        await hr.db.execute("UPDATE hr_users SET role = 'admin', active = true WHERE id IN ($1, $2)", a, b)
        r1, r2 = await asyncio.gather(
            hr.patch(f"/api/hr/users/{b}", json={"role": "hr"}, headers=as_("admin")),
            hr.patch(f"/api/hr/users/{a}", json={"active": False}, headers=dev(8202)))
        assert sorted([r1.status_code, r2.status_code])[0] == 200 and not (r1.status_code == r2.status_code == 200)
        assert await hr.db.fetchval("SELECT count(*) FROM hr_users WHERE role = 'admin' AND active") >= 1
        loser = r1 if r1.status_code != 200 else r2
        assert loser.json()["error"]["code"] in {"forbidden", "hr_last_admin", "hr_self_change"}


# ---------------------------------------------------------------- Cache-Control: no-store and the rate limit

async def test_every_hr_answer_is_no_store(hr):
    ok = await hr.get("/api/hr/me", headers=as_("hr"))
    answers = [
        ok,
        await hr.get("/api/hr/users", headers=as_("admin")),
        await hr.post("/api/hr/users", json={"tgId": 9200, "role": "hr"}, headers=as_("admin")),
        await hr.get("/api/hr/me"),  # 401
        await hr.get("/api/hr/users", headers=as_("hr")),  # 403
        await hr.patch("/api/hr/users/999999", json={"role": "hr"}, headers=as_("admin")),  # 404
        await hr.patch(f"/api/hr/users/{hr.ids['admin']}", json={"role": "hr"}, headers=as_("admin")),  # 409
        await hr.post("/api/hr/users", json={}, headers=as_("admin")),  # 422
        await hr.get("/api/hr/nope", headers=as_("admin")),  # 404 from the router
    ]
    assert [r.status_code for r in answers] == [200, 200, 201, 401, 403, 404, 409, 422, 404]
    assert all(r.headers["cache-control"] == NO_STORE for r in answers), [r.headers.get("cache-control") for r in answers]
    # not applied elsewhere
    assert "cache-control" not in (await hr.get("/health")).headers
    assert "cache-control" not in (await hr.get("/api/me", headers=as_("hr"))).headers


async def test_no_store_middleware_scope():
    seen = []

    async def app(scope, receive, send):
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    mw = NoStoreMiddleware(app)

    async def call(path, typ="http"):
        sent = []

        async def send(m):
            sent.append(m)
        await mw({"type": typ, "path": path}, None, send)
        return dict(sent[0].get("headers", [])) if sent and sent[0]["type"] == "http.response.start" else None

    assert (await call("/api/hr"))[b"cache-control"] == b"no-store"
    assert (await call("/api/hr/me"))[b"cache-control"] == b"no-store"
    assert (await call("/api/hrx")) == {} and (await call("/api/me")) == {} and seen == []


async def test_hr_routes_are_rate_limited_per_user(make_client, clean_db, clean_redis, fake_sim):
    c = await make_client(sim=fake_sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL,
                          rate_limit_overrides={"hr": 3})
    await add_hr(clean_db, TG["hr"], "hr")
    await add_hr(clean_db, TG["admin"], "admin")
    codes = [(await c.get("/api/hr/me", headers=as_("hr"))).status_code for _ in range(5)]
    assert codes == [200, 200, 200, 429, 429]
    r = await c.get("/api/hr/me", headers=as_("hr"))
    err = assert_error(r, 429, "rate_limited")
    assert err["details"]["limit"] == 3 and int(r.headers["retry-after"]) >= 1
    assert r.headers["cache-control"] == NO_STORE
    assert (await c.get("/api/hr/me", headers=as_("admin"))).status_code == 200  # another user has their own window
    assert (await c.get("/api/me", headers=as_("hr"))).status_code == 200  # other routes are not on the `hr` limit


async def test_cors_allows_patch_for_the_dev_web_client(hr):
    r = await hr.options("/api/hr/users/1", headers={
        "Origin": "http://localhost:5173", "Access-Control-Request-Method": "PATCH",
        "Access-Control-Request-Headers": "authorization,content-type"})
    assert r.status_code == 200 and "PATCH" in r.headers["access-control-allow-methods"]


# ---------------------------------------------------------------- PII masking

CARD = {"id": 7, "fullName": "Ivanova Anna", "phone": "+79991112233", "email": "a@example.com", "telegram": "anna",
        "birthDate": "2003-05-01", "hrNote": "call after 18:00", "tgUsername": "anna", "university": "MGUPP", "status": "new",
        "oa": 120, "tags": ["senior"]}


def actor(role: str) -> HrActor:
    return HrActor(id=1, tg_id=1, user_id=None, name="x", role=role)


@pytest.mark.parametrize("role", ["hr", "hr_lead", "admin"])
def test_mask_pii_keeps_everything_for_pii_roles(role):
    out = mask_pii(CARD, actor(role))
    assert out == {**CARD, "piiMasked": False}


@pytest.mark.parametrize("who", [actor("analyst"), None])
def test_mask_pii_masks_for_analyst_and_anonymous(who):
    out = mask_pii(CARD, who)
    assert out["piiMasked"] is True
    for key in ("phone", "email", "telegram", "birthDate", "hrNote", "tgUsername"):
        assert out[key] is None, key
    assert out["fullName"] == "Кандидат #7"
    # everything else is untouched, and the input is never mutated
    assert (out["university"], out["status"], out["oa"], out["tags"], out["id"]) == ("MGUPP", "new", 120, ["senior"], 7)
    assert CARD["phone"] == "+79991112233" and CARD["fullName"] == "Ivanova Anna" and "piiMasked" not in CARD


def test_mask_pii_snake_case_extra_and_missing_keys():
    out = mask_pii({"id": 3, "full_name": "A B", "birth_date": "2000-01-01", "phone": None, "city": "Moscow"},
                   actor("analyst"), extra=["city"])
    assert out == {"id": 3, "full_name": "Кандидат #3", "birth_date": None, "phone": None, "city": None, "piiMasked": True}
    assert "fullName" not in out  # absent keys are not invented
    assert mask_pii({"phone": "1"}, actor("analyst")) == {"phone": None, "piiMasked": True}
    assert mask_pii({"fullName": "A"}, actor("analyst"))["fullName"] is None  # no id: no placeholder number


def test_mask_pii_many():
    rows = [{"id": 1, "fullName": "A", "phone": "1"}, {"id": 2, "fullName": "B", "phone": "2"}]
    masked = mask_pii_many(rows, actor("analyst"))
    assert [r["fullName"] for r in masked] == ["Кандидат #1", "Кандидат #2"] and all(r["phone"] is None for r in masked)
    assert [r["phone"] for r in mask_pii_many(rows, actor("hr"))] == ["1", "2"]


# ---------------------------------------------------------------- lead_events

async def make_lead(db) -> int:
    season = await db.fetchval("INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7) RETURNING id")
    user = await db.fetchval("INSERT INTO users (tg_id, first_name) VALUES (9500, 'Cand') RETURNING id")
    return await db.fetchval(
        "INSERT INTO leads (user_id, season_id, full_name, university, consent, consent_at, consent_text_version) "
        "VALUES ($1, $2, 'Cand Idate', 'MGUPP', true, now(), 'v1') RETURNING id", user, season)


@pytest_asyncio.fixture
async def sm(hr):
    engine = create_async_engine(TEST_DB_URL)
    yield async_sessionmaker(engine, expire_on_commit=False)
    await engine.dispose()


async def test_log_event_in_the_callers_transaction(hr, sm):
    lead = await make_lead(hr.db)
    a = HrActor(id=hr.ids["hr"], tg_id=TG["hr"], user_id=None, name="HR", role="hr")
    async with sm() as session, session.begin():
        ev = await log_event(session, lead, a, "status", {"from": "new", "to": "interview"})
        assert ev.id and ev.created_at
    row = await hr.db.fetchrow("SELECT * FROM lead_events WHERE id = $1", ev.id)
    assert (row["lead_id"], row["actor_hr_id"], row["kind"]) == (lead, hr.ids["hr"], "status")
    assert row["payload"] in ('{"to": "interview", "from": "new"}', '{"from": "new", "to": "interview"}')
    # it rolls back together with the change it describes
    with pytest.raises(RuntimeError):
        async with sm() as session, session.begin():
            await log_event(session, lead, a, "note", {"len": 5})
            raise RuntimeError("boom")
    assert await hr.db.fetchval("SELECT count(*) FROM lead_events") == 1


async def test_log_event_kinds_system_actor_and_defaults(hr, sm):
    lead = await make_lead(hr.db)
    assert EVENT_KINDS == ("view", "status", "assign", "note", "message", "export", "tag")
    async with sm() as session, session.begin():
        for kind in EVENT_KINDS:
            await log_event(session, lead, None, kind)  # system actor, no payload
        with pytest.raises(ValueError):
            await log_event(session, lead, None, "delete")
    rows = await hr.db.fetch("SELECT kind, actor_hr_id, payload FROM lead_events ORDER BY id")
    assert [r["kind"] for r in rows] == list(EVENT_KINDS)
    assert all(r["actor_hr_id"] is None and r["payload"] == "{}" for r in rows)


async def test_record_event_and_history_order(hr, sm):
    lead = await make_lead(hr.db)
    a = HrActor(id=hr.ids["admin"], tg_id=TG["admin"], user_id=None, name="A", role="admin")
    for kind in ("view", "assign", "tag"):
        await record_event(sm, lead, a, kind, {"i": kind})
    rows = await hr.db.fetch("SELECT kind FROM lead_events WHERE lead_id = $1 ORDER BY created_at, id", lead)
    assert [r["kind"] for r in rows] == ["view", "assign", "tag"]
    plan = await hr.db.fetch("EXPLAIN SELECT * FROM lead_events WHERE lead_id = 1 ORDER BY created_at")
    assert plan  # the (lead_id, created_at) index exists (checked by name in the migration test)


async def test_lead_events_foreign_keys(hr, sm):
    lead = await make_lead(hr.db)
    a = HrActor(id=hr.ids["hr"], tg_id=TG["hr"], user_id=None, name="HR", role="hr")
    await record_event(sm, lead, a, "view")
    await hr.db.execute("DELETE FROM hr_users WHERE id = $1", hr.ids["hr"])  # the history survives its author
    assert await hr.db.fetchval("SELECT actor_hr_id FROM lead_events WHERE lead_id = $1", lead) is None
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await hr.db.execute("INSERT INTO lead_events (lead_id, kind) VALUES (999999, 'view')")
    await hr.db.execute("DELETE FROM leads WHERE id = $1", lead)  # ... and goes with its lead
    assert await hr.db.fetchval("SELECT count(*) FROM lead_events") == 0
