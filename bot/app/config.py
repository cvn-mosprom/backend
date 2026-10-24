"""Bot runtime configuration (env vars, see docs/ARCHITECTURE.md and .env.example)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    bot_token: str = ""
    # Public https URL of the Mini App (the web build served by caddy). Empty -> /start answers without a button.
    webapp_url: str = ""
    log_level: str = "INFO"
    app_env: str = "dev"
    # Background jobs call the api's /internal/* in-network (caddy never proxies it); the token is the api's INTERNAL_TOKEN.
    api_internal_url: str = "http://api:8000"
    internal_token: str = ""
    # S6 (HR bot): the bot reads the database directly (never factories.state). Empty DATABASE_URL -> HR features idle.
    database_url: str = ""
    pgbouncer: bool = False
    # Telegram ids allowed to use the HR commands / status buttons; lead cards go to hr_chat_id, or to every admin.
    hr_admin_tg_ids: frozenset[int] = frozenset()
    hr_chat_id: int | None = None
    bots_enabled: bool = True  # demo bots visible in /top (marked), same flag as the api
    # S6 (player side): the api's per-minute request counters (metrics:req:* / metrics:5xx:*) feed the 5xx alert. Empty -> no alert.
    redis_url: str = ""

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> Settings:
        e = os.environ if env is None else env
        domain = e.get("DOMAIN", "").strip()
        webapp = e.get("WEBAPP_URL", "").strip()
        if not webapp and domain:
            # DOMAIN may be a bare host (localhost, game.example.com) or already carry a scheme.
            webapp = domain if domain.startswith(("http://", "https://")) else f"https://{domain}"
        return cls(
            bot_token=e.get("BOT_TOKEN", "").strip(),
            webapp_url=webapp.rstrip("/") if webapp else "",
            log_level=e.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            app_env=e.get("APP_ENV", "dev").strip().lower() or "dev",
            api_internal_url=(e.get("API_INTERNAL_URL", "").strip() or "http://api:8000").rstrip("/"),
            internal_token=e.get("INTERNAL_TOKEN", "").strip(),
            database_url=e.get("DATABASE_URL", "").strip(),
            pgbouncer=_flag(e.get("PGBOUNCER"), False),
            hr_admin_tg_ids=parse_id_list(e.get("HR_ADMIN_TG_IDS")),
            hr_chat_id=_opt_int(e.get("HR_CHAT_ID")),
            bots_enabled=_flag(e.get("BOTS_ENABLED"), True),
            redis_url=e.get("REDIS_URL", "").strip(),
        )


def _flag(raw: str | None, default: bool) -> bool:
    v = (raw or "").strip().lower()
    return default if not v else v not in ("0", "false", "no", "off")


def _opt_int(raw: str | None) -> int | None:
    try:
        return int(raw.strip()) if raw and raw.strip() else None
    except ValueError:
        return None


def parse_id_list(raw: str | None) -> frozenset[int]:
    """Comma / space separated Telegram ids; garbage entries are ignored."""
    out: set[int] = set()
    for part in (raw or "").replace(";", ",").replace(" ", ",").split(","):
        n = _opt_int(part)
        if n is not None:
            out.add(n)
    return frozenset(out)
