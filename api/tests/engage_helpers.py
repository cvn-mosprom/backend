"""Shared fixtures and builders for the S5 tests (leads, quiz, events).

`engage` = client on the throwaway Postgres + Redis, rate limiter OFF, and a sim that is DOWN and counts every attempt:
the routes under test must never touch it (asserted at teardown of every test that uses the fixture).
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from typing import Any

import httpx
import pytest_asyncio

from app import quiz_bank
from app.leads import today as leads_today
from app.sim_client import SimClient
from tests.helpers import TEST_DB_URL, TEST_REDIS_URL, dev


class DownSim:
    """Every call fails with a connection error; `attempts` records them."""

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def client(self) -> SimClient:
        def handler(request: httpx.Request) -> httpx.Response:
            self.attempts.append(request.url.path)
            raise httpx.ConnectError("sim is down (test)", request=request)

        return SimClient("http://sim.test", 1.0, transport=httpx.MockTransport(handler))


@pytest_asyncio.fixture
async def engage_factory(make_client, clean_db, clean_redis):
    """`await engage_factory(**settings)` -> client (.db asyncpg, .redis, .sim DownSim)."""
    sims: list[DownSim] = []

    async def _make(**overrides: Any) -> httpx.AsyncClient:
        sim = DownSim()
        sims.append(sim)
        overrides.setdefault("rate_limit_enabled", False)
        client = await make_client(sim=sim.client(), database_url=TEST_DB_URL, redis_url=TEST_REDIS_URL, **overrides)
        client.db = clean_db  # type: ignore[attr-defined]
        client.redis = clean_redis  # type: ignore[attr-defined]
        client.sim = sim  # type: ignore[attr-defined]
        return client

    yield _make
    assert all(s.attempts == [] for s in sims), f"sim was called: {[s.attempts for s in sims]}"


@pytest_asyncio.fixture
async def engage(engage_factory):
    return await engage_factory()


# ---------------------------------------------------------------- builders

def years_ago(years: int, days: int = 0) -> str:
    """ISO date `years` full years (and `days` more) before today."""
    t = leads_today()  # the same (UTC) day the API uses for the 18+ check
    try:
        d = t.replace(year=t.year - years)
    except ValueError:  # 29 Feb
        d = t.replace(year=t.year - years, day=28)
    return (d - timedelta(days=days)).isoformat()


def lead_body(**over: Any) -> dict[str, Any]:
    """The case form with the pre-S9 field names (telegram, direction, vacancy, consentTextVersion)."""
    body: dict[str, Any] = {
        "fullName": "Иванов Иван Иванович", "birthDate": years_ago(21), "phone": "+7 916 123-45-67",
        "email": "ivan@example.com", "telegram": "@ivan_petrov", "university": "РХТУ им. Менделеева",
        "direction": "Химическая технология", "course": "4", "vacancy": "lab", "readiness": "season",
        "practiceType": "paid", "consent": True, "consentTextVersion": "2026-10-v1"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def client_body(**over: Any) -> dict[str, Any]:
    """The client's LeadForm exactly as vendor/frontend buildLead() sends it (http-contract.md, POST /api/leads)."""
    body: dict[str, Any] = {
        "fullName": "Иван Иванов", "birthDate": years_ago(21), "phone": "+79990000000", "email": "a@b.ru",
        "tgUsername": "ivan_ivanov", "university": "МГУ", "institutionKind": "vuz", "course": "3", "department": "lab",
        "roleId": "operator", "readyFrom": "2027-03-01", "practiceType": "paid", "consent": True,
        "consentVersion": "2026-10-24"}
    body.update(over)
    return {k: v for k, v in body.items() if v is not None}


def legacy_body(**over: Any) -> dict[str, Any]:
    """What the vendored LeadForm.svelte sends today (+ the consent text version HttpGameApi adds)."""
    body: dict[str, Any] = {"fullName": "Пётр Сидоров", "contact": "@petr_sidorov", "university": "МГТУ им. Баумана",
                            "course": "2", "roleId": "operator", "consent": True, "consentTextVersion": "2026-10-v1"}
    body.update(over)
    return body


async def post_lead(c, tg: int = 1, **over: Any):
    return await c.post("/api/leads", json=lead_body(**over), headers=dev(tg))


def js(v: Any) -> Any:
    return json.loads(v) if isinstance(v, (str, bytes)) else v


async def outbox(c, kind: str | None = None) -> list[dict[str, Any]]:
    rows = await c.db.fetch("SELECT * FROM outbox WHERE ($1::text IS NULL OR kind = $1) ORDER BY id", kind)
    return [{**dict(r), "payload": js(r["payload"])} for r in rows]


async def user_id(c, tg: int = 1) -> int:
    return await c.db.fetchval("SELECT id FROM users WHERE tg_id = $1", tg)


async def seed_factory(c, tg: int = 1, *, stars: int = 0, snapshot: dict[str, Any] | None = None,
                       n_season: int = 1) -> int:
    """User `tg` (created through the API) + an open season + a factory row with a given snapshot (no sim involved).
    Returns the season id."""
    r = await c.get("/api/me", headers=dev(tg))
    assert r.status_code == 200, r.text
    uid = await user_id(c, tg)
    sid = await c.db.fetchval("SELECT id FROM seasons WHERE NOT closed ORDER BY id DESC LIMIT 1")
    if sid is None:
        sid = await c.db.fetchval(
            "INSERT INTO seasons (starts_at, ends_at, days) VALUES (now(), now() + interval '7 days', 7) RETURNING id")
    await c.db.execute(
        "INSERT INTO factories (user_id, season_id, name, state, stars, snapshot) VALUES ($1, $2, 'Plant', '{}'::jsonb, $3, $4::jsonb)",
        uid, sid, stars, json.dumps(snapshot) if snapshot is not None else None)
    return sid


def correct_answers(n: int | None = None, *, index: bool = False) -> list[dict[str, Any]]:
    """Right answers for the first n questions: the option id ('b', what the client sends) or the 0-based index."""
    return [{"question_id": q.id, "answer": q.correct if index else q.correct_id} for q in quiz_bank.QUESTIONS[:n]]


def wrong_answers(n: int | None = None, *, index: bool = False) -> list[dict[str, Any]]:
    out = []
    for q in quiz_bank.QUESTIONS[:n]:
        w = (q.correct + 1) % len(q.options)
        out.append({"question_id": q.id, "answer": w if index else q.option_ids[w]})
    return out
