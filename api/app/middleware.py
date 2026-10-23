"""Request id, JSON access log, last-resort 500 handler (pure ASGI, one pass)."""
from __future__ import annotations

import contextvars
import logging
import re
import sys
import time
import uuid
from datetime import datetime, timezone

import orjson
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .errors import error_response

access_log = logging.getLogger("mosprom.access")
app_log = logging.getLogger("mosprom.app")

request_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("request_id", default=None)
_RID_OK = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
QUIET_PATHS = {"/health", "/ready"}  # probes: DEBUG unless they fail


class JsonFormatter(logging.Formatter):
    """One JSON object per line. Structured fields go in `extra={"fields": {...}}`."""

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "ts": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        out.update(getattr(record, "fields", None) or {})
        if "request_id" not in out and (rid := request_id_var.get()):
            out["request_id"] = rid
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)  # stdout only, never in responses
        return orjson.dumps(out, default=str).decode()


def setup_logging(level: str = "INFO") -> None:
    """Idempotent: JSON lines on stdout; leaves foreign handlers (pytest caplog) alone."""
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in [h for h in root.handlers if getattr(h, "_mosprom", False)]:
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    handler._mosprom = True  # type: ignore[attr-defined]
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore"):  # one INFO line per sim call would drown the access log
        logging.getLogger(noisy).setLevel(logging.WARNING)


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        incoming = Headers(scope=scope).get("x-request-id")
        rid = incoming if incoming and _RID_OK.match(incoming) else str(uuid.uuid4())
        state = scope.setdefault("state", {})  # shared with request.state
        state["request_id"] = rid
        token = request_id_var.set(rid)
        started = time.perf_counter()
        status: int | None = None

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                MutableHeaders(scope=message)["X-Request-ID"] = rid
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception:
                app_log.exception("unhandled exception", extra={"fields": {"request_id": rid}})
                state["error_code"] = "internal_error"
                if status is not None:
                    raise  # response already started: nothing sane to send
                await error_response("internal_error", "Internal server error", 500)(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
            self._log(scope, state, rid, status, started)

    @staticmethod
    def _log(scope: Scope, state: dict, rid: str, status: int | None, started: float) -> None:
        path = scope.get("path", "")
        level = logging.INFO
        if status is not None and status >= 500:
            level = logging.ERROR
        elif path in QUIET_PATHS:
            level = logging.DEBUG
        access_log.log(level, "request", extra={"fields": {
            "request_id": rid,
            "user_id": state.get("user_id"),
            "tg_id": state.get("tg_id"),
            "method": scope.get("method"),
            "path": path,
            "status": status,
            "error_code": state.get("error_code"),
            "duration_ms": round((time.perf_counter() - started) * 1000, 2),
        }})
