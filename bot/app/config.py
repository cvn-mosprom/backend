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
        )
