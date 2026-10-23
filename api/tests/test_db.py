"""Migrations (real `alembic` CLI from api/), models <-> schema drift, pgbouncer-safe engine, JSONB round trip."""
from __future__ import annotations

import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import text

from app.db import connect_args, make_engine, make_sessionmaker
from app.models import Base, Factory, Season, User
from tests.helpers import API_DIR, TEST_DB_URL, build_settings


def alembic(*args: str, url: str = TEST_DB_URL) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "alembic", *args], cwd=API_DIR, capture_output=True, text=True,
                          env={**os.environ, "DATABASE_URL": url})


def test_alembic_upgrade_head_and_back(pg_url):
    down = alembic("downgrade", "base")
    assert down.returncode == 0, down.stderr
    up = alembic("upgrade", "head")
    assert up.returncode == 0, up.stderr
    cur = alembic("current")
    assert "(head)" in cur.stderr + cur.stdout


def test_alembic_requires_database_url(pg_url):
    env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
    res = subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=API_DIR, env=env,
                         capture_output=True, text=True)
    assert res.returncode != 0 and "DATABASE_URL" in res.stderr


async def test_models_match_migrations(pg_url):
    engine = make_engine(build_settings(), url=pg_url)

    def diff(sync_conn):
        ctx = MigrationContext.configure(sync_conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        assert await conn.run_sync(diff) == []
    await engine.dispose()


async def test_schema_shape(pg_url):
    engine = make_engine(build_settings(), url=pg_url)
    async with engine.connect() as conn:
        cols = {(r[0], r[1]): r[2] for r in await conn.execute(text(
            "SELECT table_name, column_name, data_type FROM information_schema.columns WHERE table_schema='public'"))}
        idx = {r[0] for r in await conn.execute(text("SELECT indexname FROM pg_indexes WHERE schemaname='public'"))}
    await engine.dispose()
    assert cols[("users", "id")] == "bigint" and cols[("users", "tg_id")] == "bigint"
    assert cols[("factories", "state")] == "jsonb" and cols[("factories", "quality_avg")] == "real"
    assert cols[("factories", "credits")] == "bigint" and cols[("seasons", "id")] == "integer"
    assert {"ix_factories_season_stars", "ix_factories_season_quality", "uq_users_tg_id"} <= idx
    for col in ("tg_id", "username", "first_name", "last_name", "age_confirmed", "age_confirmed_at", "university",
                "source", "ref_by", "is_bot", "created_at", "last_seen_at"):
        assert ("users", col) in cols, col


async def test_models_roundtrip_and_constraints(clean_db):
    from sqlalchemy.exc import IntegrityError

    engine = make_engine(build_settings(), url=TEST_DB_URL)
    sm = make_sessionmaker(engine)
    now = datetime.now(timezone.utc)
    state = {"money": 5, "nested": {"a": [1, 2, {"b": None}]}, "meta": {"idempotency": []}, "кириллица": "да"}
    async with sm() as s, s.begin():
        user = User(tg_id=2**40, username="u")  # tg_id beyond int32
        season = Season(starts_at=now, ends_at=now + timedelta(days=7), days=7)
        s.add_all([user, season])
        await s.flush()
        s.add(Factory(user_id=user.id, season_id=season.id, name="Plant", state=state))
    async with sm() as s:
        f = (await s.execute(text("SELECT 1"))).scalar_one()
        assert f == 1
        fac = await s.get(Factory, 1)
        assert fac.state == state and fac.version == 0 and fac.level == 1 and fac.stars == 0
        u = await s.get(User, 1)
        assert u.tg_id == 2**40 and u.is_bot is False and u.age_confirmed is False and u.created_at.tzinfo
    with pytest.raises(IntegrityError):  # tg_id is unique
        async with sm() as s, s.begin():
            s.add(User(tg_id=2**40))
    await engine.dispose()


def test_pgbouncer_connect_args():
    plain = connect_args(build_settings(pgbouncer=False))
    assert "statement_cache_size" not in plain and "prepared_statement_cache_size" not in plain
    pooled = connect_args(build_settings(pgbouncer=True))
    assert pooled["statement_cache_size"] == 0 and pooled["prepared_statement_cache_size"] == 0
    names = {pooled["prepared_statement_name_func"]() for _ in range(3)}
    assert len(names) == 3  # unique prepared statement names per call


async def test_pgbouncer_mode_engine_works(pg_url):
    engine = make_engine(build_settings(pgbouncer=True), url=pg_url)
    async with engine.connect() as conn:
        for i in range(3):  # repeated identical statements: would reuse a prepared statement without the flags
            assert (await conn.execute(text("SELECT :i + 1"), {"i": i})).scalar_one() == i + 1
    await engine.dispose()
