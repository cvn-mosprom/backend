"""Invariants every response of the API must satisfy, whatever the request was (checked on EVERY response of a SmokeApi)."""

import re

import httpx

RID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
CODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
# fragments that only appear when a stack trace / driver / framework internals leak into a body
LEAKS = ("Traceback (most recent call last)", "site-packages", '.py", line', "/app/app/", "asyncpg", "sqlalchemy", "psycopg",
         "starlette", "uvicorn", "DETAIL:")
# the only 5xx that are an intended, documented answer (ARCHITECTURE.md "Error format"); 500 is never acceptable
DOCUMENTED_503 = {"sim_unavailable", "db_unavailable", "redis_unavailable", "config_error", "timeout", "service_unavailable"}


def error_shape_problems(body: object) -> list[str]:
    """`{"error": {"code", "message", "details"}}` and nothing else (S8: a rejected command also has a top-level `snapshot`)."""
    allowed = ({"error"}, {"error", "snapshot"})
    if not isinstance(body, dict) or set(body) not in allowed or not isinstance(body.get("snapshot", {}), dict):
        return [f"error body must be exactly {{'error': ...}}, got {str(body)[:120]!r}"]
    e = body["error"]
    if not isinstance(e, dict) or set(e) != {"code", "message", "details"}:
        return [f"error keys must be code/message/details, got {str(e)[:120]!r}"]
    out = []
    if not (isinstance(e["code"], str) and CODE_RE.match(e["code"])):
        out.append(f"error.code is not snake_case: {e['code']!r}")
    if not (isinstance(e["message"], str) and e["message"]):
        out.append("error.message is empty")
    if not isinstance(e["details"], dict):
        out.append("error.details is not an object")
    return out


def problems(r: httpx.Response, allow_503: bool = False) -> list[str]:
    """Everything wrong with one response ([] = clean). `allow_503`: documented 503 codes are fine (degraded-mode tests)."""
    out: list[str] = []
    rid = r.headers.get("x-request-id")
    if not rid or not RID_RE.match(rid):
        out.append(f"X-Request-ID missing or malformed: {rid!r}")
    text = r.text
    if r.status_code == 204:
        if r.content:
            out.append("204 with a body")
    elif r.content and not r.headers.get("content-type", "").startswith("application/json"):
        out.append(f"non-JSON content-type {r.headers.get('content-type')!r}")
    for leak in LEAKS:
        if leak in text:
            out.append(f"leaks internals: {leak!r} in the body")
    if r.status_code == 500:
        out.append(f"server error {r.status_code}: {text[:160]!r}")
    if r.status_code >= 400 and r.status_code != 204:
        try:
            body = r.json()
        except ValueError:
            out.append(f"{r.status_code} body is not JSON: {text[:120]!r}")
            return out
        out += error_shape_problems(body)
        code = body.get("error", {}).get("code") if isinstance(body, dict) and isinstance(body.get("error"), dict) else None
        if 500 <= r.status_code < 600 and r.status_code != 500:
            if not allow_503 or r.status_code != 503 or code not in DOCUMENTED_503:
                out.append(f"unexpected {r.status_code} {code}")
        if r.status_code == 429:
            ra = r.headers.get("retry-after", "")
            if not (ra.isdigit() and int(ra) >= 1):
                out.append(f"429 without a usable Retry-After: {ra!r}")
    return out
