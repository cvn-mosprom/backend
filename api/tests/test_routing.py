import pytest


@pytest.mark.asyncio
async def test_probe_is_anonymous_uncached_and_does_not_require_dependencies(make_client):
    client = await make_client(region_id="kzn", release_id="release-1")
    response = await client.get("/routing/ping?nonce=test")
    assert response.status_code == 200
    assert response.json() == {"regionId": "kzn", "releaseId": "release-1", "apiVersion": 1}
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.asyncio
async def test_probe_supports_browser_cors_without_telegram_auth(make_client):
    client = await make_client(cors_origins="https://game.example.com")
    response = await client.get("/routing/ping", headers={"Origin": "https://game.example.com"})
    assert response.headers["access-control-allow-origin"] == "https://game.example.com"
    response = await client.get("/routing/ping", headers={"Origin": "https://evil.example.org"})
    assert "access-control-allow-origin" not in response.headers


@pytest.mark.asyncio
async def test_old_frontend_cannot_mutate_new_release(make_client):
    client = await make_client(app_env="prod", frontend_revision="current", internal_token="routing-test-" + "x" * 32)
    for headers in ({}, {"X-Frontend-Revision": "old"}):
        response = await client.post("/api/command", json={}, headers=headers)
        assert response.status_code == 426
        assert response.json()["error"]["code"] == "client_update_required"
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/routing/ping")).status_code == 200
