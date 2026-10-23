"""E2E client helpers for the running compose stack (Caddy -> api -> sim, Postgres, Redis).

Env: E2E_BASE (default https://localhost, TLS verification off - Caddy's local CA), E2E_ORDER_WAIT_S (default 120).
Every test gets a fresh dev user (random Telegram id, `Authorization: dev <id>`) = a fresh factory.
"""

import os
import random
import time
import uuid
from typing import Any

import httpx
import pytest

BASE = os.environ.get("E2E_BASE", "https://localhost").rstrip("/")


class Api:
    """One browser tab: its own connection and its own idea of the factory version (like HttpGameApi)."""

    def __init__(self, tg_id: int, name: str = "e2e") -> None:
        self.tg_id = tg_id
        self.name = name
        self.http = httpx.Client(
            base_url=BASE,
            verify=False,
            timeout=30.0,
            headers={"Authorization": f"dev {tg_id}:{name}"},
        )
        self.version: int | None = None
        self.throttled = 0  # number of 429 answers swallowed by retries
        self._children: list[Api] = []

    def close(self) -> None:
        self.http.close()
        for c in self._children:
            c.close()

    def clone(self) -> "Api":
        """Another tab / a page reload of the same user."""
        child = Api(self.tg_id, self.name)
        self._children.append(child)
        return child

    # ---- transport ----
    def request(self, method: str, path: str, **kw: Any) -> httpx.Response:
        for attempt in range(4):
            r = self.http.request(method, path, **kw)
            if r.status_code != 429 or attempt == 3:
                return r
            self.throttled += 1
            time.sleep(min(float(r.headers.get("Retry-After", "1")), 10.0))
        raise AssertionError("unreachable")

    # ---- endpoints ----
    def bootstrap(self) -> dict[str, Any]:
        r = self.request("GET", "/api/bootstrap")
        assert r.status_code == 200, r.text
        body = r.json()
        self.version = body["version"]
        return body

    def me(self) -> dict[str, Any]:
        r = self.request("GET", "/api/factory/me")
        assert r.status_code == 200, r.text
        return r.json()

    def advance(self, **body: Any) -> httpx.Response:
        return self.request("POST", "/api/advance", json=body)

    def raw_command(self, cmd: dict[str, Any], version: int | None = None, key: str | None = None) -> httpx.Response:
        body = {
            "version": self.version if version is None else version,
            "idempotency_key": key or uuid.uuid4().hex,
            "cmd": cmd,
        }
        return self.request("POST", "/api/command", json=body)

    def command(self, cmd: dict[str, Any], expect_ok: bool = True) -> dict[str, Any]:
        """Send with the tracked version and a fresh key; on 200 the tracked version moves to the response version."""
        r = self.raw_command(cmd)
        if expect_ok:
            assert r.status_code == 200, f"{cmd['type']}: {r.status_code} {r.text}"
        if r.status_code == 200:
            body = r.json()
            assert body["ok"] is True
            self.version = body["version"]
            return body
        return r.json()


def err(r: httpx.Response) -> dict[str, Any]:
    """The `error` object of a failed response."""
    assert r.status_code >= 400, r.text
    return r.json()["error"]


def layout_of(snapshot: dict[str, Any]) -> dict[str, Any]:
    return snapshot["factory"]["layout"]


def order_of(snapshot: dict[str, Any], order_id: str) -> dict[str, Any] | None:
    return next((o for o in snapshot["orders"] if o["id"] == order_id), None)


def building(snapshot: dict[str, Any], kind: str) -> dict[str, Any]:
    return next(b for b in layout_of(snapshot)["buildings"] if b["kind"] == kind)


@pytest.fixture
def new_user():
    """Factory of fresh dev users (each a new random tg id); all connections are closed at the end."""
    made: list[Api] = []

    def make() -> Api:
        a = Api(random.randint(10**9, 10**10 - 1))
        made.append(a)
        return a

    yield make
    for a in made:
        a.close()


@pytest.fixture
def api(new_user) -> Api:
    return new_user()


def pytest_report_header(config: pytest.Config) -> str:
    return f"e2e base: {BASE}"
