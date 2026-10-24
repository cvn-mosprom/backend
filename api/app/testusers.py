"""Test users (S13): who counts as a test player, and how to find / purge them.

A dev-auth caller is a TEST user when its name (`Authorization: dev <tg_id>:<name>`, or `X-Dev-User: <tg_id>:<name>`) starts
with "e2e" (case-insensitive: the e2e helpers send "e2e"), or its tg_id is in the reserved range `>= TEST_TG_ID_MIN`
(10**12; real Telegram ids are far below, bots are negative). Telegram (tma) callers are never test users, and dev auth
does not exist in prod. The flag is kept on `users.is_test` and copied to `factories.is_test`; test users never appear in the
leaderboard, ranks, season results, HR funnel / sources / stats / export / top, reminders or HR lead cards.
Remove them before the demo with `python -m app.purge_test_users`.
"""
from __future__ import annotations

TEST_TG_ID_MIN = 10**12
TEST_NAME_PREFIX = "e2e"


def is_test_identity(tg_id: int, name: str | None) -> bool:
    return tg_id >= TEST_TG_ID_MIN or (name or "").strip().lower().startswith(TEST_NAME_PREFIX)
