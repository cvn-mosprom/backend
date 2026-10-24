"""S13 against the running stack: caddy serves the Mini App web build on the same origin as the API, and the e2e players
(dev auth name "e2e", users.is_test) are kept out of the top. DB assertions use `docker compose exec db psql` (skipped when that fails)."""

from __future__ import annotations

import httpx
import pytest
from conftest import BASE, Api, err
from test_s5_leads_quiz_events import sql, uid_of


@pytest.fixture(scope="module")
def web() -> httpx.Client:
    with httpx.Client(base_url=BASE, verify=False, timeout=30.0) as c:
        yield c


def test_root_serves_prerendered_landing_html(web):
    r = web.get("/")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "<!doctype html" in r.text.lower() and "/_app/" in r.text  # the SvelteKit landing
    assert r.headers["cache-control"] == "no-cache"
    assert 'id="hero-heading"' in r.text


@pytest.mark.parametrize("path", ["/app", "/play", "/privacy", "/some/client/route"])
def test_spa_routes_fall_back_to_html(web, path):
    r = web.get(path)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")


def test_hashed_assets_are_cached_forever(web):
    import re

    asset = re.search(r'"(/_app/immutable/[^"]+\.js)"', web.get("/").text)
    assert asset, "no hashed asset referenced from index.html"
    r = web.get(asset.group(1))
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert "javascript" in r.headers["content-type"]


def test_api_health_and_internal_are_not_the_spa(web):
    assert web.get("/health").json()["status"] == "ok"
    r = web.get("/api/nope")
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/json")
    assert err(r)["code"]
    for path in ("/internal/seasons/rotate", "/internal"):
        assert web.get(path).status_code == 404
        assert web.post(path).status_code == 404


def test_bootstrap_with_the_clients_dev_header_on_the_same_origin(web):
    r = web.get("/api/bootstrap", headers={"X-Dev-User": "424242"})
    assert r.status_code == 200 and "snapshot" in r.json()


def test_e2e_player_is_flagged_and_not_in_the_top(api: Api):
    api.bootstrap()
    uid = uid_of(api)
    assert sql(f"SELECT is_test FROM users WHERE id = {uid}") == "t"
    assert sql(f"SELECT is_test FROM factories WHERE user_id = {uid}") == "t"
    board = api.http.get("/api/season/leaderboard", params={"tab": "general", "limit": 100}).json()
    assert str(uid) not in [row["playerId"] for row in board["rows"]]
    assert board["me"]["playerId"] == str(uid)  # the player still sees their own place


def test_events_batch_with_an_unknown_name_is_partially_accepted(api: Api):
    api.bootstrap()
    r = api.http.post("/api/events", json={"events": [{"name": "app_open"}, {"name": "not_an_event"}, {"name": "game_start"}]})
    assert r.status_code == 200
    assert (r.json()["accepted"], r.json()["rejected"]) == (2, 1)
    assert sql(f"SELECT count(*) FROM events WHERE user_id = {uid_of(api)}") == "2"


@pytest.mark.parametrize("path", ["/landing", "/landing/"])
def test_legacy_landing_redirects_to_root(web, path):
    response = web.get(path)
    assert response.status_code == 308
    assert response.headers["location"] == "/"
