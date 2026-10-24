"""Unified error format: {"error": {"code", "message", "details"}} + exception handlers."""
from __future__ import annotations

import logging
from typing import Any

import orjson
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy.exc import DBAPIError, InterfaceError, OperationalError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import Response

log = logging.getLogger("mosprom.errors")

STATUS_BY_CODE: dict[str, int] = {
    "unauthorized": 401, "invalid_init_data": 401, "auth_expired": 401, "invalid_code": 401,
    "login_expired": 410,
    "age_required": 403, "consent_required": 403, "forbidden": 403,
    "not_found": 404,
    "version_conflict": 409, "lead_already_exists": 409, "quiz_already_completed": 409,
    "validation_error": 422, "command_rejected": 422,
    "rate_limited": 429,
    "internal_error": 500,
    "sim_unavailable": 503, "db_unavailable": 503, "redis_unavailable": 503, "config_error": 503,
}
_HTTP_CODES = {
    400: "bad_request", 401: "unauthorized", 403: "forbidden", 404: "not_found", 405: "method_not_allowed",
    413: "payload_too_large", 429: "rate_limited", 500: "internal_error", 503: "service_unavailable",
}


def status_for(code: str) -> int:
    """401/403/404(*_not_found)/409/429/5xx per ARCHITECTURE.md; any other code defaults to 422."""
    if code in STATUS_BY_CODE:
        return STATUS_BY_CODE[code]
    return 404 if code.endswith("_not_found") else 422


class AppError(Exception):
    """Raise anywhere; rendered as the unified error body. `status` defaults from `code`."""

    def __init__(
        self,
        code: str,
        message: str | None = None,
        *,
        status: int | None = None,
        details: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        body_extra: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message or code.replace("_", " ").capitalize()
        self.status = status if status is not None else status_for(code)
        self.details = details or {}
        self.headers = headers or {}
        self.body_extra = body_extra or {}  # top-level siblings of `error` (e.g. the web client's `snapshot` on a 422)
        super().__init__(f"{code}: {self.message}")


def error_response(
    code: str, message: str, status: int, details: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None, body_extra: dict[str, Any] | None = None,
) -> Response:
    headers = dict(headers or {})
    details = dict(details or {})
    if status == 429:
        if not any(k.lower() == "retry-after" for k in headers):
            headers["Retry-After"] = "1"  # 429 always carries Retry-After ...
        retry = next(v for k, v in headers.items() if k.lower() == "retry-after")
        if "retry_after" not in details and str(retry).isdigit():
            details["retry_after"] = int(retry)  # ... and mirrors it in details.retry_after (http-contract.md)
    body = orjson.dumps({"error": {"code": code, "message": message, "details": details}, **(body_extra or {})})
    return Response(body, status_code=status, media_type="application/json", headers=headers)


def _mark(request: Request, code: str) -> None:
    request.state.error_code = code  # picked up by the access log


async def _app_error(request: Request, exc: AppError) -> Response:
    _mark(request, exc.code)
    return error_response(exc.code, exc.message, exc.status, exc.details, exc.headers, exc.body_extra)


async def _validation_error(request: Request, exc: RequestValidationError) -> Response:
    _mark(request, "validation_error")
    errors = [{"loc": list(e.get("loc", ())), "msg": str(e.get("msg", "")), "type": str(e.get("type", ""))}
              for e in exc.errors()]
    return error_response("validation_error", "Request validation failed", 422, {"errors": errors})


async def _http_error(request: Request, exc: StarletteHTTPException) -> Response:
    code = _HTTP_CODES.get(exc.status_code, "http_error")
    _mark(request, code)
    message = exc.detail if isinstance(exc.detail, str) else code.replace("_", " ").capitalize()
    return error_response(code, message, exc.status_code, headers=dict(exc.headers or {}))


async def _db_unavailable(request: Request, exc: Exception) -> Response:
    log.warning("database unavailable: %s", type(exc).__name__)
    _mark(request, "db_unavailable")
    return error_response("db_unavailable", "Database is unavailable", 503)


# SQLSTATEs that mean "the database could not serve this in time / right now": 57014 query_canceled (statement_timeout),
# 55P03 lock_not_available (lock_timeout), 57P01-03 admin shutdown / crash recovery, 53300 too_many_connections.
_DB_REASONS = {"57014": "statement_timeout", "55P03": "lock_timeout", "57P01": "admin_shutdown",
               "57P02": "crash_shutdown", "57P03": "cannot_connect_now", "53300": "too_many_connections"}


# SQLSTATEs of text the database cannot store: 22021 invalid byte sequence (NUL), 22P05 untranslatable \u0000 in jsonb.
_DB_BAD_TEXT = {"22021", "22P05"}


async def _db_error(request: Request, exc: DBAPIError) -> Response:
    """Statement / lock timeouts and a restarting database -> 503 db_unavailable; NUL / unstorable text that slipped past
    validation -> 422 validation_error; any other DB error stays a 500."""
    state = getattr(exc.orig, "sqlstate", None) or getattr(exc.orig, "pgcode", None)
    if state in _DB_BAD_TEXT:
        log.warning("database rejected request text (%s)", state)
        _mark(request, "validation_error")
        return error_response("validation_error", "Request contains characters that cannot be stored", 422)
    reason = _DB_REASONS.get(state or "")
    if reason is None and (state or "").startswith("08"):
        reason = "connection_error"
    if reason is None:
        return await _internal_error(request, exc)
    log.warning("database error %s (%s)", state, reason)
    _mark(request, "db_unavailable")
    return error_response("db_unavailable", "Database is unavailable", 503, {"reason": reason},
                          headers={"Retry-After": "1"})


async def _timeout_error(request: Request, exc: TimeoutError) -> Response:
    """asyncpg `command_timeout` (PGBOUNCER mode) surfaces as a bare TimeoutError from the driver."""
    log.warning("database command timed out")
    _mark(request, "db_unavailable")
    return error_response("db_unavailable", "Database is unavailable", 503, {"reason": "statement_timeout"},
                          headers={"Retry-After": "1"})


async def _redis_unavailable(request: Request, exc: Exception) -> Response:
    log.warning("redis unavailable: %s", type(exc).__name__)
    _mark(request, "redis_unavailable")
    return error_response("redis_unavailable", "Redis is unavailable", 503)


async def _internal_error(request: Request, exc: Exception) -> Response:
    """Fallback for ServerErrorMiddleware; RequestContextMiddleware normally gets there first."""
    log.error("unhandled exception", exc_info=exc)
    _mark(request, "internal_error")
    return error_response("internal_error", "Internal server error", 500)


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, _app_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    app.add_exception_handler(StarletteHTTPException, _http_error)
    app.add_exception_handler(OperationalError, _db_unavailable)
    app.add_exception_handler(InterfaceError, _db_unavailable)
    app.add_exception_handler(ConnectionError, _db_unavailable)
    app.add_exception_handler(DBAPIError, _db_error)
    app.add_exception_handler(TimeoutError, _timeout_error)
    app.add_exception_handler(RedisConnectionError, _redis_unavailable)
    app.add_exception_handler(RedisTimeoutError, _redis_unavailable)
    app.add_exception_handler(Exception, _internal_error)
