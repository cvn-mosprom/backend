from __future__ import annotations

from app.settings import Settings, normalize_db_url


def test_env_vars_are_read(monkeypatch):
    monkeypatch.setenv("APP_ENV", "prod")
    monkeypatch.setenv("DEV_AUTH", "1")
    monkeypatch.setenv("PGBOUNCER", "1")
    monkeypatch.setenv("SIM_TIMEOUT_S", "2.5")
    monkeypatch.setenv("INIT_DATA_MAX_AGE_S", "600")
    monkeypatch.setenv("RATE_LIMIT_OVERRIDES", '{"command": 999}')
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@db/x")
    s = Settings()
    assert s.is_prod and s.dev_auth is True and s.dev_auth_enabled is False
    assert s.pgbouncer is True and s.sim_timeout_s == 2.5 and s.init_data_max_age_s == 600
    assert s.rate_limit_overrides == {"command": 999}
    assert s.database_url == "postgresql+asyncpg://u:p@db/x"  # driver forced to asyncpg


def test_defaults_are_dev_safe():
    s = Settings()
    assert s.app_env == "dev" and s.dev_auth is False and s.pgbouncer is False
    assert s.init_data_max_age_s == 86400 and s.sim_timeout_s == 3.0


def test_normalize_db_url():
    assert normalize_db_url("postgres://a/b") == "postgresql+asyncpg://a/b"
    assert normalize_db_url("postgresql+asyncpg://a/b") == "postgresql+asyncpg://a/b"
