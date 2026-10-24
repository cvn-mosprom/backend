"""Bot entry point: ``python -m app.main``.

Starts the APScheduler (no jobs yet) and aiogram long polling. With an empty / malformed BOT_TOKEN the
process logs a warning and idles instead of crash-looping, so `docker compose up` stays green locally.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
from datetime import datetime, timezone

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.utils.token import TokenValidationError

from .config import Settings
from .handlers import build_router
from .hr import runtime as hr_runtime
from .jobs import build_scheduler, register_jobs
from .player import runtime as player_runtime

log = logging.getLogger("bot")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "service": "bot",
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level)
    # the 7 s outbox job would otherwise log two "Running job / executed successfully" lines per run (errors still show)
    if level != "DEBUG":
        logging.getLogger("apscheduler.executors.default").setLevel(logging.WARNING)


def make_bot(settings: Settings) -> Bot | None:
    """Bot instance, or None when there is no usable token (the process then idles)."""
    if not settings.bot_token:
        log.warning("BOT_TOKEN is empty: the bot will not connect to Telegram and just idles (set BOT_TOKEN in .env)")
        return None
    try:
        return Bot(
            token=settings.bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            session=AiohttpSession(proxy=settings.telegram_proxy_url) if settings.telegram_proxy_url else None,
        )
    except TokenValidationError:
        log.error("BOT_TOKEN is malformed: the bot will not connect to Telegram and just idles")
        return None


def build_dispatcher(settings: Settings) -> Dispatcher:
    dp = Dispatcher(settings=settings)  # `settings` is injected into handlers by name
    dp.include_router(build_router())
    return dp


def install_signal_handlers(stop: asyncio.Event) -> None:
    """PID 1 in a container ignores SIGTERM unless handled: translate SIGINT/SIGTERM into `stop`."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):  # Windows / non-main thread
            try:
                signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))
            except ValueError:
                pass


async def amain(settings: Settings, stop: asyncio.Event | None = None) -> int:
    stop = stop or asyncio.Event()
    install_signal_handlers(stop)

    bot = make_bot(settings)
    if settings.app_env == "prod" and bot is None:
        raise RuntimeError("Valid BOT_TOKEN required in production")

    scheduler = build_scheduler()
    register_jobs(scheduler)
    scheduler.start()
    log.info("scheduler started (env=%s)", settings.app_env)

    hr_runtime.setup(settings, bot)  # S6: DB session factory + bot for the HR commands and the outbox job
    try:
        if bot is None:
            await stop.wait()
            return 0

        dp = build_dispatcher(settings)
        polling = asyncio.create_task(dp.start_polling(bot, handle_signals=False))
        stopper = asyncio.create_task(stop.wait())
        done, _ = await asyncio.wait({polling, stopper}, return_when=asyncio.FIRST_COMPLETED)
        if polling in done:
            stopper.cancel()
            polling.result()  # surfaces e.g. TelegramUnauthorizedError -> non-zero exit, visible in `compose ps`
            return 1
        await dp.stop_polling()
        await polling
        return 0
    finally:
        scheduler.shutdown(wait=False)
        await hr_runtime.close()
        await player_runtime.close()  # redis client of the 5xx alert
        if bot is not None:
            await bot.session.close()
        log.info("bot stopped")


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    log.info("bot starting (webapp_url=%s)", settings.webapp_url or "-")
    if os.environ.get("ETCD_ENDPOINTS"):
        from .lease import run_with_lease
        raise SystemExit(asyncio.run(run_with_lease(lambda stop: amain(settings, stop))))
    raise SystemExit(asyncio.run(amain(settings)))


if __name__ == "__main__":
    main()
