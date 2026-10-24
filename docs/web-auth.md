# Web login via the Telegram bot (S14) — contract for the frontend

The web build (outside Telegram) has no accounts of its own: the player proves their Telegram identity through the bot, so
web and Mini App are the SAME user and the SAME factory (`users.tg_id`). Login ends with an opaque **session token** that the
client sends as `Authorization: Bearer <token>`; every `/api/*` endpoint then behaves exactly as with initData.

`vendor/frontend` is not ours, so the frontend lead has to add (all of it lives next to `HttpGameApi`):
1. a "Войти через Telegram" screen (below) shown when there is no `initData` and no stored token (or after a 401);
2. `HttpGameApi.headers()`: `initData` present -> `X-Telegram-Init-Data` (unchanged); else a stored token ->
   `Authorization: Bearer <token>`; else (dev only) `X-Dev-User`. `Authorization` wins over the other headers on the server;
3. token storage in `localStorage` (key e.g. `mosprom.webToken`); a logout button; a global handler: any 401 with
   `error.code` in `unauthorized | auth_expired` while a token is stored -> drop the token, show the login screen.

Errors use the usual `{"error": {"code", "message", "details"}}` body. Public endpoints are rate limited **per IP**:
`code` and `start` 5/min, `poll` ~80/min; 429 carries `Retry-After`.

## Option A — deep link (no typing, recommended)

```
POST /api/auth/web/start                          (no body)
200 {"nonce": "q9...Zg", "botUrl": "https://t.me/<bot>?start=weblogin_q9...Zg", "expiresAt": "2026-10-24T12:05:00Z"}
503 config_error                                  BOT_USERNAME is not set on the server -> fall back to option B
```
Show a button/link to `botUrl` (opens Telegram; the bot asks "Войти на сайт?" and the player confirms), then poll every ~2 s:
```
POST /api/auth/web/poll   {"nonce": "q9...Zg"}
202 {"status": "pending"}                                       keep polling
200 {"token": "...43 chars...", "expiresAt": "2026-11-23T12:00:00Z",
     "user": {"id": 5, "tgId": 777001, "name": "Ann Lee", "username": "ann"}}      take it ONCE
410 {"error": {"code": "login_expired", ...}}                    expired (5 min), unknown, or already collected -> restart with /start
```
The token is returned by exactly one poll; a second poll (or a lost response) gives 410, so just start over.

## Option B — 6-digit code (also works while `BOT_USERNAME` is unset)

The player sends `/login` to the bot, which answers with a code (valid 5 min, one use; a new `/login` kills the previous code).
```
POST /api/auth/web/code   {"code": "123456"}
200 {"token": "...", "expiresAt": "...", "user": {...}}           same shape as poll
401 {"error": {"code": "invalid_code"}}                           wrong / expired / already used (indistinguishable on purpose)
429 rate_limited                                                  5/min per IP, and 5 wrong codes lock the IP for up to 10 min
```
Spaces inside the code are ignored; anything that is not 6 digits is `invalid_code`.

## Using the session

```
GET /api/bootstrap          Authorization: Bearer <token>         (any /api/* endpoint)
GET /api/auth/web/session   -> 200 {"user": {...}, "expiresAt": "..."}      validate a stored token at app start
DELETE /api/auth/web/session -> 204                                          logout (this browser only)
```
- Lifetime 30 days, **sliding**: the server renews it (at most once an hour) while the player keeps using the game.
- 401 `auth_expired` = the session ran out, 401 `unauthorized` = unknown or revoked token (e.g. the player sent `/logout_web`
  to the bot, which kills every web session). Both mean: delete the stored token and show the login screen.
- CORS: `Authorization` is already allowed; the served web build is same-origin anyway.
- Never put the token in a URL; keep it only in `localStorage` and the `Authorization` header.

## Server side (for reference)

Internal endpoints used by the bot (header `X-Internal-Token`, not reachable through Caddy):
`POST /internal/auth/login-code {tg_id, username?, first_name?, last_name?}` -> `{code, expiresAt}`;
`POST /internal/auth/approve {nonce, tg_id, username?, first_name?, last_name?}` -> `{ok: true}` | 410 `login_expired`;
`POST /internal/auth/revoke-all {tg_id}` -> `{revoked: n}`. Design and tables: docs/ARCHITECTURE.md "Web login via Telegram bot (S14)".
