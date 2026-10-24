"""Production checks shared by application startup and deployment preflight."""
from __future__ import annotations

from urllib.parse import urlsplit
import asyncio
import re

from sqlalchemy import text
from .db import make_engine

from .settings import Settings


def check(settings: Settings) -> list[str]:
    errors = list(settings.missing_required())
    if not settings.is_prod:
        return errors
    db = urlsplit(settings.database_url)
    if not db.password or db.password.lower() in {"mosprom", "postgres", "password", "changeme"}:
        errors.append("DATABASE_URL uses a missing/demo password")
    if db.username in {"postgres", "mosprom_migrate"}:
        errors.append("DATABASE_URL must use the application role")
    if settings.dev_auth:
        errors.append("DEV_AUTH must be disabled")
    if not settings.cors_origin_list or "*" in settings.cors_origin_list:
        errors.append("CORS_ORIGINS must contain explicit HTTPS origins")
    if any(not origin.startswith("https://") for origin in settings.cors_origin_list):
        errors.append("CORS_ORIGINS must use HTTPS")
    if not re.fullmatch(r"[0-9]{5,20}:[A-Za-z0-9_-]{20,}", settings.bot_token):
        errors.append("BOT_TOKEN format invalid")
    if not settings.webapp_url.startswith("https://"):
        errors.append("WEBAPP_URL must use HTTPS")
    return errors


async def check_role(settings: Settings) -> list[str]:
    engine = make_engine(settings)
    try:
        async with asyncio.timeout(5):
            async with engine.connect() as connection:
                row = (await connection.execute(text("SELECT rolsuper, rolcreaterole, rolcreatedb FROM pg_roles WHERE rolname=current_user"))).one()
                if any(row):
                    return ["DATABASE_URL uses an administrative database role"]
        return []
    except Exception:
        return ["DATABASE_URL role check failed"]
    finally:
        await engine.dispose()


if __name__ == "__main__":
    settings = Settings()
    problems = check(settings)
    if not problems and settings.is_prod:
        problems.extend(asyncio.run(check_role(settings)))
    for problem in problems:
        print(problem)
    raise SystemExit(1 if problems else 0)
