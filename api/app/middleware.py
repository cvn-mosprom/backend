"""Request id, JSON access log, request time cap, slow-request log, last-resort 500 handler (pure ASGI, one pass)."""
from __future__ import annotations

import asyncio
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
NO_TIMEOUT_PREFIXES = ("/internal/",)  # long background calls (top-200 catch-up, season rotation): no 5 s cap


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
    """`request_timeout_s` caps a whole request (cancels the handler, answers 503 `timeout` + Retry-After: 1 if nothing
    was sent yet); `/internal/*` is exempt. `slow_ms`: requests slower than that log at WARNING ("slow request")."""

    def __init__(self, app: ASGIApp, request_timeout_s: float = 5.0, slow_ms: float = 1000.0) -> None:
        self.app = app
        self.request_timeout_s = request_timeout_s
        self.slow_ms = slow_ms

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

        path = scope.get("path", "")
        cap = None if path.startswith(NO_TIMEOUT_PREFIXES) or self.request_timeout_s <= 0 else self.request_timeout_s

        try:
            try:
                try:
                    async with asyncio.timeout(cap) as deadline:
                        await self.app(scope, receive, send_wrapper)
                except TimeoutError:
                    if not deadline.expired():
                        raise  # a TimeoutError of the handler itself, not our cap
                    state["error_code"] = "timeout"
                    app_log.warning("request timed out after %ss", cap, extra={"fields": {"request_id": rid}})
                    if status is None:
                        await error_response("timeout", "Request took too long", 503, {"timeout_s": cap},
                                             headers={"Retry-After": "1"})(scope, receive, send_wrapper)
            except Exception:
                app_log.exception("unhandled exception", extra={"fields": {"request_id": rid}})
                state["error_code"] = "internal_error"
                if status is not None:
                    raise  # response already started: nothing sane to send
                await error_response("internal_error", "Internal server error", 500)(scope, receive, send_wrapper)
        finally:
            request_id_var.reset(token)
            self._log(scope, state, rid, status, started)

    def _log(self, scope: Scope, state: dict, rid: str, status: int | None, started: float) -> None:
        path = scope.get("path", "")
        duration_ms = round((time.perf_counter() - started) * 1000, 2)
        slow = duration_ms > self.slow_ms > 0
        level = logging.INFO
        if status is not None and status >= 500:
            level = logging.ERROR
        elif slow:
            level = logging.WARNING
        elif path in QUIET_PATHS:
            level = logging.DEBUG
        extra: dict = {"slow": True} if slow else {}
        sim = getattr(getattr(scope.get("app"), "state", None), "sim", None)
        breaker = getattr(sim, "breaker", None)
        if breaker is not None and breaker.state != "closed":
            extra["breaker_state"] = breaker.state  # only while the sim breaker is not closed
        access_log.log(level, "slow request" if slow else "request", extra={"fields": {
            **extra,
            "request_id": rid,
            "user_id": state.get("user_id"),
            "tg_id": state.get("tg_id"),
            "method": scope.get("method"),
            "path": path,
            "status": status,
            "error_code": state.get("error_code"),
            "duration_ms": duration_ms,
        }})
