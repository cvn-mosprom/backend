# Backend architecture (shared contract for all workers)

The source of truth for the product spec is `docs/spec-rev4.txt` (sprint plan, revision 4).
This file pins down the interfaces between the parts so they can be built in parallel.

## Repo layout and ownership

```
vendor/frontend/      git submodule cvn-mosprom/frontend, pinned commit. READ-ONLY. Never edit.
  web/src/lib/...     lib/sim, lib/api/types.ts, lib/api/mock/MockGameApi.ts (alias $lib -> vendor/frontend/web/src/lib)
api/                  FastAPI service (Python 3.12). Package: api/app. Tests: api/tests. Alembic: api/alembic.
api/app/contract/generated/   GENERATED Pydantic models (owned by contract/ generator, never hand-edited)
sim/                  Node/TS sim service, imports vendor/frontend/web/src/lib without changes.
bot/                  aiogram 3 bot + APScheduler (all background jobs live here).
contract/             schema generation scripts + generated JSON Schemas (contract/schema/*.json).
docker-compose.yml, Caddyfile, .env.example   infra (root).
docs/                 this file, PROGRESS.md at root.
```

Python: always `py -3.12` (the default `py` is 3.14). Each Python part has its own venv: `api/.venv`, `bot/.venv`, `contract/.venv`.
Node: v24 locally, image `node:20-alpine` or newer LTS in Docker. Package manager: npm.

## Error format (every API error)

```json
{"error": {"code": "version_conflict", "message": "human readable", "details": {"current_version": 7}}}
```
HTTP status by code: 401 unauthorized|invalid_init_data|auth_expired; 403 age_required|consent_required;
404 *_not_found; 409 version_conflict|lead_already_exists|quiz_already_completed; 422 validation_error|command_rejected|...;
429 rate_limited (+ `Retry-After` header always); 503 sim_unavailable|db_unavailable|redis_unavailable|config_error; 500 internal_error.
Every response carries `X-Request-ID` (echo incoming header or generate uuid4).

## Auth

- Prod: header `Authorization: tma <raw initData>`; HMAC-SHA256 check per Telegram Mini Apps spec
  (secret = HMAC_SHA256(key="WebAppData", msg=BOT_TOKEN)); `auth_date` older than `INIT_DATA_MAX_AGE_S` (default 86400) -> 401 auth_expired;
  bad hash/format -> 401 invalid_init_data; missing header -> 401 unauthorized. Short Redis cache of verified initData (key = sha256 of raw string, TTL 300 s).
- Dev: when `DEV_AUTH=1`, header `Authorization: dev <tg_id>[:<name>]` is accepted. Never enabled in prod (`APP_ENV=prod` forces it off).

## Env vars

`APP_ENV` (dev|prod), `DATABASE_URL` (postgresql+asyncpg://...), `REDIS_URL`, `SIM_URL` (http://sim:8080), `SIM_TIMEOUT_S` (3),
`BOT_TOKEN`, `DEV_AUTH` (0|1), `INIT_DATA_MAX_AGE_S`, `PGBOUNCER` (0|1 -> statement_cache_size=0 + prepared_statement_cache_size=0),
`DOMAIN` (caddy), `LOG_LEVEL`.

## Sim service HTTP API (internal, JSON, port 8080)

The factory state is an opaque JSON object (`factories.state` JSONB). Today it is exactly the mock's `MockState` serialised,
plus top-level fields the sim does not know (e.g. `meta.idempotency`, owned by the API). **The sim MUST round-trip unknown
top-level fields untouched** (covered by a contract test). Times are ms since epoch, server time.

| Method/path | Body | Response |
|---|---|---|
| `GET /health` | – | `{"status":"ok","service":"sim","simCommit":"<vendor/frontend sha>"}` |
| `POST /v1/bootstrap` | `{state: object\|null, now, player: {id, tgId?, name, username?}}` | `{state, snapshot}` — creates a fresh state when null; lazy offline catch-up (cap 8 h) like `MockGameApi.bootstrap` |
| `POST /v1/command` | `{state, now, cmd}` | `{state, result: {ok, error?, toast?, rewards?}, snapshot}` — `result.ok=false` means rejected by game rules (API maps to 422 command_rejected) |
| `POST /v1/advance` | `{state, now, capHours?, reason?}` | `{state, snapshot, advancedMs}` |
| `POST /v1/snapshot` | `{state, now}` | `{snapshot}` — pure read, no state change |

Sim errors: 400 `{"error":{"code":"bad_request",...}}` for malformed input, 500 for crashes. The API treats timeout (3 s) or connection error as 503 sim_unavailable.
Snapshot shape = `GameSnapshot` from `vendor/frontend/web/src/lib/api/types.ts`.

## Generated contract

`contract/generate.(sh|mjs)` -> `contract/schema/command.schema.json`, `contract/schema/snapshot.schema.json` (ts-json-schema-generator over
`Command` and `GameSnapshot`) -> datamodel-code-generator -> `api/app/contract/generated/{command,snapshot}.py`.
`contract/check.(sh|mjs)` regenerates into a temp dir and fails on diff (used in CI/pre-commit).

## Local ports (dev compose)

caddy 80/443, api 8000 (internal), sim 8080 (internal), db published on 127.0.0.1:55432, redis on 127.0.0.1:56379.
API tests use a throwaway Postgres/Redis: `TEST_DATABASE_URL` (default postgresql+asyncpg://mosprom:mosprom@127.0.0.1:55433/mosprom_test)
and `TEST_REDIS_URL` (default redis://127.0.0.1:56380/0).

## S2 decisions (game endpoints)

- **Version** (`factories.version`) increments only on a successful player command with effect. Lazy catch-up (bootstrap,
  factory/me, advance, background top-200, and the catch-up part of a rejected command) persists state but does NOT bump the
  version, so passive sync in one tab never causes 409 in another tab. `version_conflict` comes only from POST /api/command.
- **Time:** API sends the sim `now = max(server_now_ms, state.lastTick)` (monotonic per factory).
- **Sim rejection codes:** sim `/v1/command` returns `result = {ok:false, error:<ru message>, code:<reason>}`. Reasons:
  `daily_limit_exceeded`, `minigame_too_short`, `impossible_score`, `history_unavailable`, `tick_removed`, `season_closed`,
  `insufficient_funds`, `not_found`, `rejected` (generic fallback). API mapping: reasons `daily_limit_exceeded`,
  `minigame_too_short`, `impossible_score` become the top-level error code (422); everything else is 422 `command_rejected`
  with `details.reason`. Details always include `message`, `version` and the fresh `snapshot`.
- **POST /api/command** body `{version:int, idempotency_key:str(8..64), cmd:Command}`; success 200
  `{ok:true, version, result:{toast?, rewards?}, events:[], snapshot, replayed:false}`.
  Replay (same key): 200 with the stored short result, current version, fresh snapshot, `replayed:true` — checked BEFORE the
  version check. Keys live in `state.meta.idempotency` (deque, last 50: `{key, ok, code?, version, result, at}`), written in the
  same transaction as the state; Redis `idem:{user_id}:{key}` TTL 600 s is only a fast-path cache.
  Rejected commands are also recorded (a replay returns the same rejection).
- **buildEnd:** auth + rate limit, read version without lock, 200 `{ok:true, version, result:{}, events:[], snapshot:null, replayed:false}`; no write, no idempotency.
- **tick / undo / redo:** rejected by sim (`tick_removed`, `history_unavailable`) -> 422 command_rejected.
- **GET /api/bootstrap:** creates factory on first call (INSERT .. ON CONFLICT DO NOTHING, then SELECT .. FOR UPDATE), ensures an
  open season row exists (creates season 1 if none; rotation is S4), catch-up with 8 h cap, returns
  `{version, serverTime, season:{id, startsAt, endsAt}, features:{undo:false, serverOrderEta:false}, snapshot}`.
- **GET /api/factory/me:** catch-up under lock, `{version, serverTime, snapshot, degraded:false}`; sim down -> last stored snapshot
  (`factories.snapshot` JSONB column) with `degraded:true` (200).
- **POST /api/advance** body `{cap_hours: 0..8 (default 8), reason: "resume"|"manual"|"background"}` -> `{version, serverTime, advancedMs, snapshot}`.
- **Shift limit:** the counter lives in the factory state (`state.season.shiftsLeft/shiftsDay` of the persisted MockState — part of
  factories.state, decremented inside the same sim command and the same DB transaction). No Redis counter. Server season
  bounds (seasons table) are aligned in S4.
- Denormalised columns (credits, stars, level, xp, bottles_season, quality_avg, last_tick_at, updated_at) and `snapshot` are
  refreshed from the sim snapshot on every write.
- **Sim (implemented in S2):** rejections carry `result.code` (table: `sim/src/reasons.ts`); `/v1/advance` accepts `reason`, and
  `background` accumulates into an unseen offline report instead of replacing it; the sim itself enforces
  `effectiveNow = max(now, state.lastTick)` on every endpoint.

## S3 decisions

- submitShift is validated in the API (`api/app/shift.py`) before the sim call; the sim keeps its own checks as a second line.
  Top-level code `minigame_too_short` or `impossible_score`; `details.reason` is the rule name (e.g. `score_out_of_range`,
  `duration_too_long`, `win_score_too_low`). Accepted shifts are logged in table `shifts` in the same transaction.
- `features.undo` = sim `/health.historyInState` (false until S1U removes undo/redo from `REJECTED_COMMANDS`), cached 60 s.
- States over 150 KB are logged with `state_bytes`; the 150 KB audit test is xfail(strict=False) until S1U.

## S4 decisions (part A: leaderboard, visit, summary, bots)

- **Source of truth:** denormalised `factories` columns (`stars`, `quality_avg`, `speed_bph`, `university`, `is_bot`; migration 0004), refreshed
  from the sim snapshot on every write. `quality_avg` is 0 until something is produced (the sim reports 1 for an empty factory).
  `speed_bph` is derived (the snapshot has no flow rate): `snapshot.factory.bottlesPerHour` if a sim ever provides it, else
  d(produced)/d(time) between a baseline kept in `state.meta.speed` {t,p} and now (baseline moves after >= 60 s, window capped at 8 h);
  idle/new factories show 0. Follow-up (sim): expose `bottlesPerHour` in `snapshot.factory`. `users.university` follows `setUniversity`.
- **GET /api/season/leaderboard** `?tab=general|quality|speed|uni&limit=1..100(100)&university|university_id=<name>&season_id`: season default = newest open one.
  Body = `Leaderboard` of types.ts (+ `seasonId`): value = stars / quality% (0.1) / bottles per hour; `uni` ranks by stars and adds `universities`
  (sum of stars per university, top 50). Top-100 cached in Redis `lb:{season}:{tab}:{uni}:{bots}` for `LEADERBOARD_CACHE_TTL_S` (20, fail-open, user agnostic);
  `isMe` and the `me` block (place/value/isMe, `gapTo51`, `inviteCutoff` = value at place 50) are per request (one rank query, ties by user id).
  Errors: 401, 422 `invalid_tab` / `validation_error`, 404 `season_not_found`, 429 (`leaderboard`), 500.
- **GET /api/factory/{playerId}** (limit `default`): `{player:{id,name,university?}, factory:{name,level,stars,layout,updatedAt(ms)}}`; layout =
  `state.sim.layout` (bots: stored layout). 404 `player_not_found` (bad id, no factory, bot while BOTS_ENABLED=0). HttpGameApi fills the private
  `Factory` fields with neutral values.
- **GET /api/season/summary** (limit `default`): `SeasonSummary` of types.ts + `seasonId, credits, stars, level, quizResult:null, leadStatus:null`
  (S5 fills the last two), read from the stored snapshot/columns, no sim call. 404 `factory_not_found`.
- **snapshot.rank** is overridden with the real general `{place,total}` in every game response and in the stored snapshot (computed in the
  write transaction with the new stars). `BOTS_ENABLED=0` hides bots from rows, ranks, totals, aggregates and visits.
- **Bots:** `python -m app.seed_bots [--count 40] [--progress P] [--floor 0.2]` (in compose: `docker compose exec api python -m app.seed_bots`).
  Idempotent; bot i = user `tg_id = -1000000 - i`, factory `state = {bot:{final,g,quality,speed,lines,lemonade}, sim:{layout}}`; `grow_bots()` is the
  function for the later growth job (monotonic inside a season, moves bots into a new season).
