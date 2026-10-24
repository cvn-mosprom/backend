"""S4 background jobs: they call the api's /internal/* (local aiohttp server as the api), log and swallow every error."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app import jobs
from app.config import Settings
from app.jobs import InternalApi, JOBS, build_scheduler, register_jobs

TOKEN = "secret-internal-token"
ROTATE = {"closed": [1], "opened": 2}
TOP = {"advanced": 3, "skipped": 1, "failed": 0, "ms": 12, "sim_down": False, "pending": 0}
GROW = {"bots": 40, "season_id": 2, "progress": 0.1}


@pytest.fixture(autouse=True)
def _reset_api():
    jobs.set_api(None)
    yield
    jobs.set_api(None)


async def with_api(test: Callable[[list[dict[str, Any]], str], Awaitable[None]], *, status: int = 200,
                   body: Any = None, delay: float = 0.0) -> None:
    """Run `test(requests, base_url)` against a fake api that records every request."""
    seen: list[dict[str, Any]] = []
    answers = {"/internal/seasons/rotate": ROTATE, "/internal/top/advance": TOP, "/internal/bots/grow": GROW}

    async def handler(request: web.Request) -> web.Response:
        raw = await request.read()
        seen.append({"path": request.path, "method": request.method, "token": request.headers.get("X-Internal-Token"),
                     "body": raw.decode()})
        if delay:
            await asyncio.sleep(delay)
        if body is not None:
            return web.Response(status=status, text=body if isinstance(body, str) else "", content_type="application/json")
        return web.json_response(answers.get(request.path, {}), status=status)

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    server = TestServer(app)
    await server.start_server()
    try:
        await test(seen, str(server.make_url("")).rstrip("/"))
    finally:
        await server.close()


def run(coro) -> None:
    asyncio.run(coro)


def test_each_job_posts_to_its_endpoint_with_the_token():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        assert await jobs.seasons_rotate() == ROTATE
        assert await jobs.top_advance() == TOP
        assert await jobs.bots_grow() == GROW
        assert [(r["method"], r["path"], r["token"]) for r in seen] == [
            ("POST", "/internal/seasons/rotate", TOKEN), ("POST", "/internal/top/advance", TOKEN),
            ("POST", "/internal/bots/grow", TOKEN)]
        assert seen[1]["body"] == '{"limit": 200}'

    run(with_api(test))


def test_jobs_swallow_api_errors_and_log_them(caplog):
    caplog.set_level(logging.INFO)

    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        for job in (jobs.seasons_rotate, jobs.top_advance, jobs.bots_grow):
            assert await job() is None  # no exception

    for status in (401, 404, 500, 503):
        run(with_api(test, status=status))
    assert "check INTERNAL_TOKEN" in caplog.text and "HTTP 500" in caplog.text


@pytest.mark.parametrize("body", ["not json", "[1, 2]", ""])
def test_jobs_swallow_a_garbage_answer(body):
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        assert await jobs.bots_grow() is None

    run(with_api(test, body=body))


def test_jobs_swallow_a_dead_api():
    async def test():
        jobs.set_api(InternalApi("http://127.0.0.1:9", TOKEN))  # nothing listens there
        for job in (jobs.seasons_rotate, jobs.top_advance, jobs.bots_grow):
            assert await job() is None

    run(test())


def test_a_timeout_is_swallowed():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))
        assert await jobs.call_internal("slow", "/internal/bots/grow", timeout_s=0.1) is None
        assert len(seen) == 1

    run(with_api(test, delay=0.5))


def test_no_token_means_no_request(caplog):
    async def test(seen, url):
        jobs.set_api(InternalApi(url, ""))
        assert await jobs.seasons_rotate() is None
        assert seen == []

    caplog.set_level(logging.WARNING)
    run(with_api(test))
    assert "INTERNAL_TOKEN is not set" in caplog.text


def test_api_client_is_built_from_the_environment(monkeypatch):
    s = Settings.from_env({"API_INTERNAL_URL": "http://api.local:9000/", "INTERNAL_TOKEN": " tok "})
    assert (s.api_internal_url, s.internal_token) == ("http://api.local:9000", "tok")
    d = Settings.from_env({})
    assert (d.api_internal_url, d.internal_token) == ("http://api:8000", "")
    monkeypatch.setenv("API_INTERNAL_URL", "http://env.test:1")
    monkeypatch.setenv("INTERNAL_TOKEN", "from-env")
    api = InternalApi.from_env()
    assert (api.base_url, api.token) == ("http://env.test:1", "from-env")


def test_registered_jobs_are_single_instance_and_coalesced():
    async def test():
        scheduler = build_scheduler()
        assert register_jobs(scheduler) == ["seasons_rotate", "top_advance", "bots_grow"]
        by_id = {j.id: j for j in scheduler.get_jobs()}
        every = {i: j.trigger.interval.total_seconds() for i, j in by_id.items()}
        assert every == {"seasons_rotate": 60, "top_advance": 12 * 60, "bots_grow": 10 * 60}
        assert by_id["top_advance"].trigger.jitter  # spread so replicas / restarts do not align
        assert all(j.max_instances == 1 and j.coalesce for j in by_id.values())
        assert [j.id for j in JOBS] == list(by_id)

    run(test())


def test_the_scheduler_keeps_running_while_the_api_fails():
    async def test(seen, url):
        jobs.set_api(InternalApi(url, TOKEN))  # every call is answered with HTTP 500

        scheduler = build_scheduler()
        register_jobs(scheduler, [jobs.JobSpec("t", jobs.seasons_rotate, "interval", {"seconds": 0.1}, "test")])
        scheduler.start()
        try:
            await asyncio.sleep(0.8)
            assert scheduler.running and len(seen) >= 4  # kept firing after every failure
        finally:
            scheduler.shutdown(wait=False)

    run(with_api(test, status=500))
