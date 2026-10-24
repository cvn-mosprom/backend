"""C5: achievement points (ОД) and the top-3 achievements in the HR bot (`/leads` rows, `/lead` card). Real Postgres (hr_support.py)."""

from __future__ import annotations

from app.hr import handlers, queries
from app.hr.format import ACHIEVEMENT_TITLES, render_achievements, render_card, render_leads_page
from hr_support import ADMIN, add_lead, fake_message, reply_text, run_db, x
from test_hr import _lead_id, cmd


async def earn(s, uid: int, ach: str, tier: str, points: int, season_key: int = 0) -> None:
    await x(s, "INSERT INTO achievements_earned (user_id, achievement_id, season_key, tier, points, catalog_version, granted_via) "
               "VALUES (:u, :a, :k, :t, :p, 1, 'hook')", u=uid, a=ach, k=season_key, t=tier, p=points)


async def score(s, uid: int, season: int, points: int) -> None:
    await x(s, "INSERT INTO player_scores (user_id, season_id, achievement_points) VALUES (:u, :s, :p)", u=uid, s=season, p=points)


async def give(sm) -> dict[str, int]:
    """u1 (lead 'Иванова Анна'): 180 points and five achievements; u2: a player with no score row; landing lead without a player."""
    async with sm() as s:
        uid = {n: await x(s, "SELECT id FROM users WHERE first_name = :n", n=n) for n in ("Анна", "Борис", "Вера")}
    async with sm() as s, s.begin():
        await score(s, uid["Анна"], 2, 180)
        await score(s, uid["Анна"], 1, 5000)  # an old season row: the factory sits in season 2, so this one is not hers now
        await earn(s, uid["Анна"], "season_top3", "gold", 100, season_key=2)
        await earn(s, uid["Анна"], "season_top3", "gold", 100, season_key=1)  # the same seasonal one again: counted once
        await earn(s, uid["Анна"], "no_defects", "silver", 30)
        await earn(s, uid["Анна"], "museum_all", "bronze", 20)
        await earn(s, uid["Анна"], "first_line", "bronze", 10)
        await score(s, uid["Вера"], 2, 300)  # her lead is revoked
        await earn(s, uid["Вера"], "quiz_8", "gold", 40)
        await add_lead(s, None, 2, "Лендингов Лев", "МИФИ")
    return uid


def test_leads_page_shows_points_only_for_players():
    async def go(sm):
        await give(sm)
        m = fake_message(ADMIN)
        await handlers.on_leads(m, cmd("leads", "all"))
        rows = {line.split(" ")[1] + " " + line.split(" ")[2]: line for line in reply_text(m).splitlines() if line.startswith("/lead_")}
        assert " · 180 ОД · " in rows["Иванова Анна"]
        assert " · 0 ОД · " in rows["Петров Борис"]  # a player, nothing earned yet
        assert "ОД" not in rows["Лендингов Лев"]  # no game profile
        assert "ОД" not in rows["Сидорова Вера"] and "🚫" in rows["Сидорова Вера"]  # revoked: nothing of the game is shown

    run_db(go)


def test_lead_card_shows_points_and_top_three_by_points():
    async def go(sm):
        await give(sm)
        lid = await _lead_id(sm, "Иванова Анна")
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(lid)))
        card = reply_text(m)
        line = next(x_ for x_ in card.splitlines() if x_.startswith("🏅"))
        assert line == "🏅 ОД: <b>180</b> · 🥇 «Призёр сезона», 🥈 «Без брака», 🥉 «Музей»"
        assert "Первая линия" not in card  # only three, best first; the seasonal one is listed once
        # lead without a score row: 0 points, none listed; landing lead: no line at all
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(await _lead_id(sm, "Петров Борис"))))
        assert "🏅 ОД: <b>0</b>" in reply_text(m) and "«" not in next(x_ for x_ in reply_text(m).splitlines() if x_.startswith("🏅"))
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(await _lead_id(sm, "Лендингов Лев"))))
        assert "🏅" not in reply_text(m)
        # revoked: the notice only, no game data
        m = fake_message(ADMIN)
        await handlers.on_lead(m, cmd("lead", str(await _lead_id(sm, "Сидорова Вера"))))
        assert "🏅" not in reply_text(m) and "согласие отозвано" in reply_text(m)

    run_db(go)


def test_status_button_redraws_the_card_with_achievements():
    async def go(sm):
        await give(sm)
        async with sm() as s:
            card = await queries.fetch_lead(s, await _lead_id(sm, "Иванова Анна"))
        assert card["achievement_points"] == 180 and [a["id"] for a in card["top_achievements"]] == ["season_top3", "no_defects", "museum_all"]
        async with sm() as s:
            outcome, card = await queries.set_lead_status(s, card["id"], "interview", "hr (tg 1)")
        assert outcome == "ok" and "🏅 ОД: <b>180</b>" in render_card(card)

    run_db(go)


def test_top_achievements_query():
    async def go(sm):
        uid = (await give(sm))["Анна"]
        async with sm() as s:
            assert [(a["id"], a["points"]) for a in await queries.top_achievements(s, uid, 10)] == [
                ("season_top3", 100), ("no_defects", 30), ("museum_all", 20), ("first_line", 10)]
            assert await queries.top_achievements(s, uid, 1) == [{"id": "season_top3", "tier": "gold", "points": 100}]
            assert await queries.top_achievements(s, 10**9) == []

    run_db(go)


def test_render_helpers():
    assert render_achievements({"id": 1}) is None and render_achievements({"achievement_points": None, "top_achievements": []}) is None
    assert render_achievements({"achievement_points": 0}) == "🏅 ОД: <b>0</b>"
    odd = render_achievements({"achievement_points": 7, "top_achievements": [{"id": "<b>x</b>", "tier": "weird"}]})
    assert odd == "🏅 ОД: <b>7</b> · «&lt;b&gt;x&lt;/b&gt;»"  # unknown id shown escaped, unknown tier without an icon
    base = {"id": 3, "full_name": "A", "university": "U", "status": "new", "tags": [], "created_at": __import__("datetime").datetime.now()}
    assert "ОД" not in render_leads_page([{**base, "achievement_points": None}], 1, 0, 1, "t")
    assert "· 42 ОД ·" in render_leads_page([{**base, "achievement_points": 42}], 1, 0, 1, "t")
    assert "🏅 ОД: <b>42</b>" in render_card({**base, "achievement_points": 42})
    assert len(ACHIEVEMENT_TITLES) == 38
