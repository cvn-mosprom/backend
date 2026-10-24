"""C5 drift test: the bot (separate image, no api import) names achievements in the lead card from its own copy of the titles."""
from __future__ import annotations

import ast
from pathlib import Path

from app.achievements import catalog

BOT_FORMAT = Path(__file__).resolve().parents[2] / "bot" / "app" / "hr" / "format.py"


def test_bot_knows_the_catalog_titles():
    tree = ast.parse(BOT_FORMAT.read_text(encoding="utf-8"))
    consts = {t.id: ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              for t in n.targets if isinstance(t, ast.Name) and t.id in ("ACHIEVEMENT_TITLES", "TIER_ICONS")}
    assert consts["ACHIEVEMENT_TITLES"] == {a.id: a.title for a in catalog.CATALOG}
    assert set(consts["TIER_ICONS"]) == set(catalog.TIERS)
