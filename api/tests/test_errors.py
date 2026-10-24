"""Unified error format, request id, access log, gzip, 500 handling. No Postgres / Redis needed."""
from __future__ import annotations

import logging
import uuid

import pytest

from app.errors import AppError, status_for
from tests.helpers import dev


def assert_error(resp, status: int, code: str) -> dict:
    assert resp.status_code == status, resp.text
    body = resp.json()
    # S8: a rejected command (422) also carries the fresh `snapshot` next to `error` (the web client applies it)
    assert set(body) == {"error"} or (status == 422 and set(body) == {"error", "snapshot"})
    assert set(body["error"]) == {"code", "message", "details"}
    assert body["error"]["code"] == code
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    assert resp.headers["x-request-id"]
    return body["error"]


async def test_validation_error_format(make_client):
    c = await make_client()
    resp = await c.post("/t/echo", json={"name": 5})
    err = assert_error(resp, 422, "validation_error")
    locs = {tuple(e["loc"]) for e in err["details"]["errors"]}
    assert ("body", "name") in locs and ("body", "count") in locs
    assert all({"loc", "msg", "type"} <= set(e) for e in err["details"]["errors"])


async def test_validation_error_on_invalid_json_body(make_client):
    c = await make_client()
    resp = await c.post("/t/echo", content=b"{not json", headers={"content-type": "application/json"})
    assert_error(resp, 422, "validation_error")


async def test_unknown_route_and_method_use_error_format(make_client):
    c = await make_client()
    assert_error(await c.get("/nope"), 404, "not_found")
    assert_error(await c.post("/health"), 405, "method_not_allowed")


async def test_unexpected_exception_is_500_without_leaks(make_client, caplog):
    c = await make_client()
    with caplog.at_level(logging.INFO):
        resp = await c.get("/t/boom")
    err = assert_error(resp, 500, "internal_error")
    assert err["message"] == "Internal server error"
    for leak in ("secret internal detail", "Traceback", "RuntimeError", "helpers.py"):
        assert leak not in resp.text
    # the traceback goes to the log instead
    logged = [r for r in caplog.records if r.name == "mosprom.app"]
    assert logged and logged[0].exc_info is not None
    access = [r for r in caplog.records if r.name == "mosprom.access"][-1]
    assert access.fields["status"] == 500 and access.fields["error_code"] == "internal_error"
    assert access.levelno == logging.ERROR


@pytest.mark.parametrize("code,status", [
    ("unauthorized", 401), ("invalid_init_data", 401), ("auth_expired", 401),
    ("age_required", 403), ("consent_required", 403),
    ("factory_not_found", 404), ("lead_not_found", 404),
    ("version_conflict", 409), ("lead_already_exists", 409), ("quiz_already_completed", 409),
    ("validation_error", 422), ("command_rejected", 422), ("invalid_tab", 422),
    ("rate_limited", 429),
    ("sim_unavailable", 503), ("db_unavailable", 503), ("redis_unavailable", 503), ("config_error", 503),
    ("internal_error", 500),
])
async def test_app_error_status_by_code(make_client, code, status):
    c = await make_client()
    resp = await c.get(f"/t/apperror/{code}")
    err = assert_error(resp, status, code)
    assert err["message"] == "raised in test"
    assert err["details"] == ({"x": 1, "retry_after": 1} if status == 429 else {"x": 1})  # 429 mirrors Retry-After
    assert status_for(code) == status


async def test_429_always_has_retry_after(make_client):
    c = await make_client()
    resp = await c.get("/t/apperror/rate_limited")  # raised without explicit headers
    assert_error(resp, 429, "rate_limited")
    assert int(resp.headers["retry-after"]) >= 1


def test_app_error_defaults():
    e = AppError("version_conflict", details={"current_version": 7})
    assert (e.status, e.code, e.details) == (409, "version_conflict", {"current_version": 7})
    assert AppError("x_custom", status=418).status == 418


async def test_request_id_echoed(make_client):
    c = await make_client()
    resp = await c.get("/health", headers={"X-Request-ID": "abc-123.DEF"})
    assert resp.headers["x-request-id"] == "abc-123.DEF"
    # also on error responses
    resp = await c.get("/nope", headers={"X-Request-ID": "req-42"})
    assert resp.headers["x-request-id"] == "req-42"


async def test_request_id_generated_when_missing_or_unsafe(make_client):
    c = await make_client()
    generated = (await c.get("/health")).headers["x-request-id"]
    assert uuid.UUID(generated).version == 4
    for bad in ("has space", "x" * 200, "semi;colon"):
        rid = (await c.get("/health", headers={"X-Request-ID": bad})).headers["x-request-id"]
        assert rid != bad and uuid.UUID(rid)


async def test_access_log_fields(make_client, caplog):
    c = await make_client()
    with caplog.at_level(logging.INFO):
        await c.get("/nope", headers={"X-Request-ID": "log-1"})
        await c.get("/api/me")  # 401
    recs = [r for r in caplog.records if r.name == "mosprom.access"]
    first, second = recs[0].fields, recs[1].fields
    assert first["request_id"] == "log-1" and first["method"] == "GET" and first["path"] == "/nope"
    assert first["status"] == 404 and first["error_code"] == "not_found"
    assert isinstance(first["duration_ms"], float) and first["user_id"] is None
    assert second["status"] == 401 and second["error_code"] == "unauthorized"


async def test_health_probes_log_at_debug(make_client, caplog):
    c = await make_client()
    with caplog.at_level(logging.INFO):
        await c.get("/health")
    assert not [r for r in caplog.records if r.name == "mosprom.access"]


async def test_log_lines_are_json(make_client, capsys):
    import json

    c = await make_client()
    await c.get("/nope", headers={"X-Request-ID": "json-1"})
    lines = [json.loads(line) for line in capsys.readouterr().out.splitlines() if line.startswith("{")]
    access = [line for line in lines if line.get("logger") == "mosprom.access"]
    assert access and access[0]["request_id"] == "json-1" and access[0]["status"] == 404


async def test_gzip_for_large_json_only(make_client):
    c = await make_client()
    big = await c.get("/t/big", headers={"Accept-Encoding": "gzip"})
    assert big.headers["content-encoding"] == "gzip" and len(big.json()["data"]) == 100
    small = await c.get("/t/small", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in small.headers
    assert (await c.get("/t/big", headers={"Accept-Encoding": "identity"})).headers.get("content-encoding") is None


async def test_gzip_responses_keep_request_id(make_client):
    c = await make_client()
    resp = await c.get("/t/big", headers={"Accept-Encoding": "gzip", "X-Request-ID": "gz-1"})
    assert resp.headers["x-request-id"] == "gz-1"


async def test_db_down_maps_to_503_db_unavailable(make_client):
    c = await make_client()  # DATABASE_URL points at a closed port
    resp = await c.get("/api/me", headers=dev(7))
    assert_error(resp, 503, "db_unavailable")
