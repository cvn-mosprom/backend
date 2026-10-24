"""Manual JSON body parsing for endpoints that need their own error codes (events, quiz)."""
from __future__ import annotations

from typing import Any

import orjson
from fastapi import Request

from .errors import AppError


def invalid(field: str, msg: str, **extra: Any) -> AppError:
    """422 validation_error in the same shape as FastAPI's own validation errors (details.errors[]) plus `extra`."""
    return AppError("validation_error", "Request validation failed",
                    details={"errors": [{"loc": ["body", *filter(None, field.split("."))], "msg": msg, "type": "value_error"}], **extra})


NO_CONTROL = r"^[^\x00-\x1f\x7f]*$"  # Query/Field `pattern`: no control characters (NUL included)


def reject_nul(value: Any, field: str = "") -> None:
    """422 if any string (or dict key) nested in `value` holds NUL: Postgres text / jsonb cannot store it."""
    if isinstance(value, str):
        if "\x00" in value:
            raise invalid(field, "NUL characters are not allowed")
    elif isinstance(value, dict):
        for k, v in value.items():
            reject_nul(k, field)
            reject_nul(v, f"{field}.{k}" if field else str(k))
    elif isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            reject_nul(v, f"{field}.{i}" if field else str(i))


async def read_json(request: Request, max_bytes: int) -> Any:
    """Body -> JSON value. Oversized -> 413 payload_too_large, malformed (or nested deeper than orjson allows) -> 422."""
    body = await request.body()
    if len(body) > max_bytes:
        raise AppError("payload_too_large", f"Request body is over {max_bytes} bytes", status=413,
                       details={"max_bytes": max_bytes})
    if rb"\u0000" in body:  # Postgres text / jsonb cannot store NUL
        raise invalid("", "NUL characters are not allowed")
    try:
        return orjson.loads(body)
    except ValueError:  # orjson.JSONDecodeError
        raise invalid("", "Body must be valid JSON") from None
