"""Aiogram routers. S0 ships only /start; HR commands (/leads, /export, /funnel, ...) come in S6."""

from aiogram import Router

from . import start, weblogin


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(weblogin.router)  # S14: before start, so `/start weblogin_<nonce>` never reaches the generic /start
    root.include_router(start.router)
    from ..player.notify import router as notify_router  # S6 reminder consent: toggle callbacks, /notify, /stop

    root.include_router(notify_router)
    from ..hr.handlers import router as hr_router  # S6 HR commands (admin-only, see app/hr)

    root.include_router(hr_router)
    return root
