"""Aiogram routers. S0 ships only /start; HR commands (/leads, /export, /funnel, ...) come in S6."""

from aiogram import Router

from . import start


def build_router() -> Router:
    root = Router(name="root")
    root.include_router(start.router)
    return root
