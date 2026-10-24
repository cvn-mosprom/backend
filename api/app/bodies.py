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
