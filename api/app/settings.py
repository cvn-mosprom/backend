"""Runtime configuration (env vars, see docs/ARCHITECTURE.md)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url(url: str) -> str:
    """Accept plain postgres:// URLs and force the asyncpg driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    app_env: Literal["dev", "prod", "test"] = "dev"
    database_url: str = "postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55432/mosprom"
    redis_url: str = "redis://127.0.0.1:56379/0"
    sim_url: str = "http://127.0.0.1:8080"
    sim_timeout_s: float = 3.0
    bot_token: str = ""
    dev_auth: bool = False
    init_data_max_age_s: int = 86400
    pgbouncer: bool = False
    domain: str = ""
    log_level: str = "INFO"

    # DB pool is per worker process; 2 workers x (5 + 5) stays within "10-20 connections per API".
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_s: float = 3.0

    # Rate limiter: master switch + per-name override of the max requests per window,
    # e.g. RATE_LIMIT_OVERRIDES='{"command": 600}'.
    rate_limit_enabled: bool = True
    rate_limit_overrides: dict[str, int] = {}

    @field_validator("app_env", mode="before")
    @classmethod
    def _lower_env(cls, v: object) -> object:
        return v.strip().lower() if isinstance(v, str) else v

    @field_validator("database_url", mode="after")
    @classmethod
    def _db_url(cls, v: str) -> str:
        return normalize_db_url(v)

    @property
    def is_prod(self) -> bool:
        return self.app_env == "prod"

    @property
    def dev_auth_enabled(self) -> bool:
        """DEV_AUTH is honoured only outside prod: APP_ENV=prod forces it off."""
        return self.dev_auth and not self.is_prod

    def missing_required(self) -> list[str]:
        """Names of required env vars that are absent. In prod nothing may rely on dev defaults."""
        required = ["DATABASE_URL", "REDIS_URL", "SIM_URL"] + (["BOT_TOKEN"] if self.is_prod else [])
        missing = []
        for name in required:
            field = name.lower()
            empty = not str(getattr(self, field)).strip()
            defaulted_in_prod = self.is_prod and field != "bot_token" and field not in self.model_fields_set
            if empty or defaulted_in_prod:
                missing.append(name)
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
