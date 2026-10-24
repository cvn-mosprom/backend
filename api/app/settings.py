"""Runtime configuration (env vars, see docs/ARCHITECTURE.md)."""
from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def normalize_db_url(url: str) -> str:
    """Accept plain postgres:// URLs and force the asyncpg driver."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


# values shipped in compose / .env.example: fine for local dev, refused in prod
INTERNAL_TOKEN_PLACEHOLDERS = frozenset({"dev-internal-token", "change-me", "changeme", "change-me-internal-token"})


DEV_CORS_ORIGIN = "http://localhost:5173"  # vite dev server of the web client


def origin_of(url: str) -> str | None:
    """`https://host:port/path?q` -> `https://host:port` (what a browser sends in `Origin`); None when not a URL."""
    parts = urlsplit(url.strip())
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}".lower()


class Settings(BaseSettings):
    model_config = SettingsConfigDict(extra="ignore", case_sensitive=False)

    frontend_revision: str = ""
    region_id: str = "local"
    release_id: str = "dev"
    redis_sentinel_nodes: str = ""
    redis_master_name: str = "mosprom"
    redis_password: str = ""
    redis_sentinel_password: str = ""
    db_tls: bool = False
    db_ca_file: str = ""
    redis_tls: bool = False
    redis_ca_file: str = ""
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

    # S8 CORS. CORS_ORIGINS = comma-separated allowed origins ("*" = any). Empty: dev -> http://localhost:5173 (vite),
    # prod -> the origin of WEBAPP_URL (the Mini App web build), else https://$DOMAIN.
    cors_origins: str = ""
    webapp_url: str = ""

    # DB pool is per worker process; 2 workers x (5 + 5) stays within "10-20 connections per API".
    db_pool_size: int = 5
    db_max_overflow: int = 5
    db_connect_timeout_s: float = 3.0

    # S7 stabilisation. Timeouts (spec Блок 8): statement 3 s, lock wait 2 s, sim 3 s (SIM_TIMEOUT_S), whole request 5 s
    # (everything except /internal/*, which runs long background work). Slow requests (> SLOW_REQUEST_MS) log at WARNING.
    db_statement_timeout_ms: int = 3000
    db_lock_timeout_ms: int = 2000
    request_timeout_s: float = 5.0
    slow_request_ms: int = 1000
    # Per-process circuit breaker on the sim client: N consecutive failures -> fail fast for OPEN_S, then one probe.
    sim_breaker_threshold: int = 5
    sim_breaker_open_s: float = 10.0

    # Shared secret of /internal/* (header X-Internal-Token), used by the bot's background jobs. Never exposed by caddy.
    # Empty = the internal API is disabled (always 401). APP_ENV=prod refuses to start with an empty/placeholder value.
    internal_token: str = ""

    # Rate limiter: master switch + per-name override of the max requests per window,
    # e.g. RATE_LIMIT_OVERRIDES='{"command": 600}'.
    rate_limit_enabled: bool = True
    rate_limit_overrides: dict[str, int] = {}

    # Leaderboard (S4): demo bots (users.is_bot) are visible unless BOTS_ENABLED=0; the top-100 is cached in Redis.
    bots_enabled: bool = True
    leaderboard_cache_ttl_s: int = 20  # spec: 15-30 s

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
    def cors_origin_list(self) -> list[str]:
        """Origins for CORSMiddleware (no trailing slashes, no paths)."""
        explicit = [o.strip().rstrip("/") for o in self.cors_origins.split(",") if o.strip()]
        if explicit:
            return explicit
        if not self.is_prod:
            return [DEV_CORS_ORIGIN]
        domain = self.domain.strip()
        for raw in (self.webapp_url, domain if "://" in domain else f"https://{domain}" if domain else ""):
            if (origin := origin_of(raw)) is not None:
                return [origin]
        return []

    @property
    def dev_auth_enabled(self) -> bool:
        """DEV_AUTH is honoured only outside prod: APP_ENV=prod forces it off."""
        return self.dev_auth and not self.is_prod

    @property
    def internal_token_ok(self) -> bool:
        """A usable secret: at least 16 chars and not one of the documented placeholders."""
        t = self.internal_token.strip()
        return len(t) >= 16 and t.lower() not in INTERNAL_TOKEN_PLACEHOLDERS

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
        if self.is_prod and not self.internal_token_ok:
            missing.append("INTERNAL_TOKEN")
        return missing


@lru_cache
def get_settings() -> Settings:
    return Settings()
