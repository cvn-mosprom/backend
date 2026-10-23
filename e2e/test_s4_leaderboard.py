"""S4 part A against the running stack: leaderboard (tabs, me block, cache), public factory view, season summary, demo bots.

The demo bots are seeded once per session inside the api container (idempotent):
    docker compose exec -T api python -m app.seed_bots        (run from the repo root; the tests skip when that fails)
"""

import subprocess
from pathlib import Path

import httpx
import pytest

from conftest import BASE, Api, err

ROOT = Path(__file__).resolve().parents[1]
TABS = ["general", "quality", "speed", "uni"]
ROW_KEYS = {"place", "playerId", "name", "factoryName", "value"}
PRIVATE = ("credits", "loan", "stock", "history", "xp", "shipped", "orders", "snapshot", "tgId", "tg_id")


@pytest.fixture(scope="session", autouse=True)
def bots():
    try:
        out = subprocess.run(["docker", "compose", "exec", "-T", "api", "python", "-m", "app.seed_bots"], cwd=ROOT,
                             capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"cannot run the bot seed: {e}")
    if out.returncode != 0:
        pytest.skip(f"bot seed failed: {out.stderr[-300:]}")
    return out.stdout.strip()


def board(api: Api, tab: str = "general", **params):
    r = api.request("GET", "/api/season/leaderboard", params={"tab": tab, **params})
    assert r.status_code == 200, r.text
    return r.json()


def test_leaderboard_all_tabs_have_bots_and_my_block(api: Api):
    boot = api.bootstrap()
    for tab in TABS:
        lb = board(api, tab)
        assert lb["tab"] == tab and len(lb["rows"]) >= 30 and lb["total"] >= 31
        assert len(lb["rows"]) <= 100
        rows = lb["rows"]
        assert all(ROW_KEYS <= r.keys() for r in rows)
        assert [r["place"] for r in rows] == list(range(1, len(rows) + 1))
        values = [r["value"] for r in rows]
        assert values == sorted(values, reverse=True) and values[0] > 0
        assert all(isinstance(r["name"], str) and r["name"] and r["factoryName"] for r in rows)
        assert isinstance(lb["inviteCutoff"], (int, float)) and lb["inviteCutoff"] >= 0
        me = lb["me"]
        assert me["isMe"] is True and me["playerId"] == boot["snapshot"]["player"]["id"]
        assert 1 <= me["place"] <= lb["total"] and me["value"] == 0  # nothing produced yet (not "100 % quality")
        assert isinstance(lb["gapTo51"], (int, float)) and lb["gapTo51"] >= 0
        if me["place"] > 50 and len(rows) > 49:
            assert lb["gapTo51"] == lb["inviteCutoff"] - me["value"]
    uni = board(api, "uni")["universities"]
    assert len(uni) >= 3 and [u["value"] for u in uni] == sorted((u["value"] for u in uni), reverse=True)
    assert all(set(u) == {"name", "value"} for u in uni)
    assert "universities" not in board(api, "general")


def test_quality_values_are_percentages_and_speed_integers(api: Api):
    api.bootstrap()
    q = board(api, "quality")["rows"]
    assert all(0 < r["value"] <= 100 for r in q[:30])
    s = board(api, "speed")["rows"]
    assert all(isinstance(r["value"], int) and r["value"] > 0 for r in s[:30])


def test_my_place_agrees_with_snapshot_rank(api: Api):
    boot = api.bootstrap()
    lb = board(api)
    rank = boot["snapshot"]["rank"]
    assert rank["place"] == lb["me"]["place"] and rank["total"] == lb["total"]
    assert api.me()["snapshot"]["rank"] == rank


def test_limit_university_filter_and_cache(api: Api):
    api.bootstrap()
    assert [r["place"] for r in board(api, limit=3)["rows"]] == [1, 2, 3]
    first = board(api)
    uni = next(r["university"] for r in first["rows"] if r.get("university"))
    filtered = board(api, university=uni)
    assert 1 <= len(filtered["rows"]) <= len(first["rows"]) and {r["university"] for r in filtered["rows"]} == {uni}
    assert board(api, university_id=uni)["rows"] == filtered["rows"]
    assert "me" not in filtered  # the new player has no university
    # cached top-100: the same rows come back (a new player appearing does not change a cached ordering within the TTL)
    assert board(api)["rows"] == first["rows"]


def test_errors(api: Api):
    api.bootstrap()
    e = err(api.request("GET", "/api/season/leaderboard", params={"tab": "nope"}))
    assert e["code"] == "invalid_tab" and set(e["details"]["allowed"]) == set(TABS)
    assert api.request("GET", "/api/season/leaderboard", params={"tab": "nope"}).status_code == 422
    assert api.request("GET", "/api/season/leaderboard", params={"limit": 0}).status_code == 422
    assert api.request("GET", "/api/season/leaderboard", params={"limit": 101}).status_code == 422
    assert err(api.request("GET", "/api/season/leaderboard", params={"season_id": 999999}))["code"] == "season_not_found"
    assert err(api.request("GET", "/api/factory/987654321012"))["code"] == "player_not_found"
    assert err(api.request("GET", "/api/factory/not-a-number"))["code"] == "player_not_found"
    with httpx.Client(base_url=BASE, verify=False) as anon:  # no Authorization header
        for path in ("/api/season/leaderboard", "/api/factory/1", "/api/season/summary"):
            r = anon.get(path)
            assert r.status_code == 401 and r.json()["error"]["code"] == "unauthorized", path


def test_visit_a_bot_shows_the_public_cut(api: Api):
    api.bootstrap()
    bot = board(api)["rows"][0]
    r = api.request("GET", f"/api/factory/{bot['playerId']}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert set(body) == {"player", "factory"}
    assert body["player"]["id"] == bot["playerId"] and body["player"]["name"] == bot["name"]
    assert body["player"].get("university") == bot.get("university")
    f = body["factory"]
    assert set(f) == {"name", "level", "stars", "layout", "updatedAt"} and f["name"] == bot["factoryName"]
    assert f["stars"] == bot["value"] and f["level"] >= 1 and isinstance(f["updatedAt"], int)
    assert f["layout"]["buildings"] and {"hangar", "buildings", "conveyors"} <= f["layout"].keys()
    for word in PRIVATE:
        assert f'"{word}"' not in r.text, word


def test_visit_another_player_and_myself(new_user):
    a, b = new_user(), new_user()
    a.bootstrap()
    boot_b = b.bootstrap()
    assert b.command({"type": "renameFactory", "name": "E2E Visit Me"})["ok"] is True
    pid = boot_b["snapshot"]["player"]["id"]
    seen = a.request("GET", f"/api/factory/{pid}")
    assert seen.status_code == 200, seen.text
    v = seen.json()
    assert v["player"]["id"] == pid and v["factory"]["name"] == "E2E Visit Me"
    assert v["factory"]["layout"] == b.me()["snapshot"]["factory"]["layout"]  # the same buildings the owner sees
    assert v["factory"]["stars"] == 0 and v["factory"]["level"] == 1
    for word in PRIVATE:
        assert f'"{word}"' not in seen.text, word
    mine = b.request("GET", f"/api/factory/{pid}")
    assert mine.status_code == 200  # b.me() above refreshed updated_at, everything else is the same
    assert mine.json()["player"] == v["player"] and {**mine.json()["factory"], "updatedAt": 0} == {**v["factory"], "updatedAt": 0}


def test_season_summary_shape(api: Api):
    boot = api.bootstrap()
    # nothing played yet: the mock's defaults
    r = api.request("GET", "/api/season/summary")
    assert r.status_code == 200, r.text
    s = r.json()
    assert {"place", "total", "inInviteZone", "bottles", "quality", "lines", "skus", "bestProfession",
            "bestProfessionReason", "credits", "stars", "level", "quizResult", "leadStatus"} <= s.keys()
    assert s["place"] == boot["snapshot"]["rank"]["place"] and s["total"] >= 31
    assert s["inInviteZone"] is (s["place"] <= 50)
    assert s["bottles"] == 0 and s["quality"] == 0 and s["lines"] == 0 and s["skus"] == 0
    assert s["bestProfession"] == "operator" and isinstance(s["bestProfessionReason"], str)
    assert s["credits"] == boot["snapshot"]["factory"]["credits"] and s["stars"] == 0 and s["level"] == 1
    assert s["quizResult"] is None and s["leadStatus"] is None


def test_summary_without_a_factory_is_404(api: Api):
    assert err(api.request("GET", "/api/season/summary"))["code"] == "factory_not_found"
