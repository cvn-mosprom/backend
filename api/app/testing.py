"""Helpers for tests / manual smoke checks: build a validly signed Telegram initData.

    python -m app.testing <BOT_TOKEN> [tg_id] [first_name]   # prints the value for `Authorization: tma <...>`
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
import time
from urllib.parse import urlencode


def make_init_data(
    bot_token: str,
    user: dict | None = None,
    *,
    auth_date: int | None = None,
    start_param: str | None = None,
    **extra: str,
) -> str:
    """Raw initData string signed the way Telegram does (WebAppData HMAC). `user` defaults to a stub user."""
    user = user or {"id": 424242, "first_name": "Test", "username": "tester"}
    fields = {
        "auth_date": str(int(time.time()) if auth_date is None else auth_date),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":"), ensure_ascii=False),
        **extra,
    }
    if start_param is not None:
        fields["start_param"] = start_param
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    fields["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return urlencode(fields)


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    print(make_init_data(args[0], {"id": int(args[1]) if len(args) > 1 else 424242,
                                   "first_name": args[2] if len(args) > 2 else "Test"}))
