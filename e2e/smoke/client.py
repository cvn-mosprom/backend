"""A browser-tab client that checks every response, plays like HttpGameApi (409 -> factory/me -> retry) and runs in parallel."""

import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from conftest import Api

from .checks import problems


class SmokeApi(Api):
    """`Api` whose every response goes through `checks.problems`; violations land in `sink` (shared by all tabs of a test)."""

    def __init__(self, tg_id: int, name: str = "e2e-smoke", *, sink: list[str] | None = None, allow_503: bool = False) -> None:
        super().__init__(tg_id, name)
        self.sink: list[str] = sink if sink is not None else []
        self.allow_503 = allow_503
        self.checking = True
        self.conflicts = 0  # 409s this tab ran into
        self.http.event_hooks = {"response": [self._check]}

    def _check(self, r: httpx.Response) -> None:
        r.read()
        if self.checking:
            for p in problems(r, self.allow_503):
                self.sink.append(f"{r.request.method} {r.request.url.path}{'?' + r.request.url.query.decode() if r.request.url.query else ''}: {p}")

    def clone(self) -> "SmokeApi":
        child = SmokeApi(self.tg_id, self.name, sink=self.sink, allow_503=self.allow_503)
        self._children.append(child)
        return child

    @contextmanager
    def unchecked(self):
        """Responses inside the block are not validated (tests that EXPECT a known-bad answer assert on it themselves)."""
        self.checking = False
        try:
            yield self
        finally:
            self.checking = True

    # ---- one command attempt, no hidden retries on 409 ----
    def send(self, cmd: dict[str, Any], version: int | None = None, key: str | None = None) -> httpx.Response:
        body = {"version": self.version if version is None else version, "idempotency_key": key or uuid.uuid4().hex, "cmd": cmd}
        for _ in range(3):  # a 429 is waited out (Retry-After) and retried; the key makes that safe
            r = self.http.post("/api/command", json=body)
            if r.status_code != 429:
                return r
            self.throttled += 1
            time.sleep(min(float(r.headers.get("Retry-After", "1")), 65.0))
        return r

    def play(self, cmd: dict[str, Any], key: str | None = None, max_tries: int = 25) -> "Played":
        """What HttpGameApi does: send with the cached version; 409 -> factory/me -> retry with the SAME key."""
        key = key or uuid.uuid4().hex
        conflicts = 0
        for _ in range(max_tries):
            r = self.send(cmd, key=key)
            if r.status_code == 409:
                conflicts += 1
                self.conflicts += 1
                cur = r.json()["error"]["details"]["current_version"]
                self.version = self.me()["version"]  # resync (never 409)
                assert self.version >= cur, "factory/me is older than the version the 409 reported"
                continue
            if r.status_code == 200:
                self.version = r.json()["version"]
            elif r.status_code == 422 and "version" in r.json()["error"].get("details", {}):
                self.version = r.json()["error"]["details"]["version"]
            return Played(r, key, conflicts)
        raise AssertionError(f"{cmd['type']}: still 409 after {max_tries} tries")


@dataclass
class Played:
    response: httpx.Response
    key: str
    conflicts: int
    body: dict[str, Any] = field(init=False)

    def __post_init__(self) -> None:
        self.body = self.response.json()

    @property
    def ok(self) -> bool:
        return self.response.status_code == 200

    @property
    def version(self) -> int:
        return self.body["version"]


def par(fns: list[Callable[[], Any]], timeout: float = 120.0) -> list[Any]:
    """Run callables on threads that all start at the same instant (barrier); returns results in order, re-raises the first error."""
    barrier = threading.Barrier(len(fns))

    def run(fn: Callable[[], Any]) -> Any:
        barrier.wait(timeout=30)
        return fn()

    with ThreadPoolExecutor(len(fns)) as ex:
        futures = [ex.submit(run, fn) for fn in fns]
        return [f.result(timeout=timeout) for f in futures]


def belt(x: int, y: int) -> dict[str, Any]:
    """A one-tile belt (20 credits) away from the tutorial row 5 gates: the cheap, repeatable, order-independent command."""
    return {"type": "placeConveyor", "kind": "belt", "path": [{"x": x, "y": y, "dir": "E"}]}


BELT_COST = 20
