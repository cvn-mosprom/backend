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
- Web client (S8): `X-Telegram-Init-Data: <raw initData>` (same as `tma`) and `X-Dev-User: <tg_id>` (same as `dev`, name `Dev<id>`, dev only).
  Priority: `Authorization`, then `X-Telegram-Init-Data`, then `X-Dev-User`; the first header present wins. An empty `X-Telegram-Init-Data` -> 401 unauthorized.
- CORS (S8, `CORSMiddleware`, outermost so that 4xx/429/5xx carry the headers; a preflight never reaches auth / rate limit / timeout):
  origins from `CORS_ORIGINS` (comma-separated); empty -> dev `http://localhost:5173`, prod the origin of `WEBAPP_URL` (else `https://$DOMAIN`).
  Allowed headers `X-Telegram-Init-Data, X-Dev-User, Content-Type, Authorization`, methods `GET, POST, DELETE`, exposed `Retry-After, X-Request-ID`, max-age 600.

## Env vars

`APP_ENV` (dev|prod), `DATABASE_URL` (postgresql+asyncpg://...), `REDIS_URL`, `SIM_URL` (http://sim:8080), `SIM_TIMEOUT_S` (3),
`BOT_TOKEN`, `DEV_AUTH` (0|1), `INIT_DATA_MAX_AGE_S`, `PGBOUNCER` (0|1 -> statement_cache_size=0 + prepared_statement_cache_size=0),
`DOMAIN` (caddy), `LOG_LEVEL`, `CORS_ORIGINS`, `WEBAPP_URL`.

## Sim service HTTP API (internal, JSON, port 8080)

The factory state is a JSON object (`factories.state` JSONB): the `FactoryState` of the vendored game core
(`vendor/frontend/web/src/lib/core`, schema `v: 2`, see "S12 decisions" below), plus top-level fields the sim does not know
(e.g. `meta.idempotency`, `meta.speed`, owned by the API). **The sim MUST round-trip unknown top-level fields untouched**
(covered by tests). Every state the sim receives goes through `migrateState` first (an old v1 state is upgraded lazily).
Times are ms since epoch, server time.

| Method/path | Body | Response |
|---|---|---|
| `GET /health` | – | `{"status":"ok","service":"sim","simCommit":"<vendor/frontend sha>","historyInState":true,"stateV":2}` |
| `POST /v1/bootstrap` | `{state: object\|null, now, player: {id, tgId?, name, username?}}` | `{state, snapshot}` — creates a fresh state when null; lazy offline catch-up (cap 8 h): `freshState` / `advanceState(.., {offlineReport})` of the core |
| `POST /v1/command` | `{state, now, cmd}` | `{state, result: {ok, error?, toast?, rewards?}, snapshot}` — `result.ok=false` means rejected by game rules (API maps to 422 command_rejected) |
| `POST /v1/advance` | `{state, now, capHours?, reason?}` | `{state, snapshot, advancedMs}` |
| `POST /v1/snapshot` | `{state, now}` | `{snapshot}` — pure read, no state change |

Sim errors: 400 `{"error":{"code":"bad_request",...}}` for malformed input, 500 for crashes. The API treats timeout (3 s) or connection error as 503 sim_unavailable.
Snapshot shape = `GameSnapshot` from `vendor/frontend/web/src/lib/api/types.ts` (`snapshotOf`; no `rank`: the API sets the real one).

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
  `daily_limit_exceeded`, `minigame_too_short`, `impossible_score`, `season_closed`,
  `insufficient_funds`, `not_found`, `rejected` (generic fallback; `history_unavailable` / `tick_removed` are gone since S12/S11). API mapping: reasons `daily_limit_exceeded`,
  `minigame_too_short`, `impossible_score` become the top-level error code (422); everything else is 422 `command_rejected`
  with `details.reason`. Details always include `message`, `version` and the fresh `snapshot`.
- **POST /api/command** body `{version:int, idempotency_key:str(8..64), cmd:Command}`; success 200
  `{ok:true, version, result:{toast?, rewards?}, events:[], snapshot, replayed:false}`.
  Replay (same key): 200 with the stored short result, current version, fresh snapshot, `replayed:true` — checked BEFORE the
  version check. Keys live in `state.meta.idempotency` (deque, last 50: `{key, ok, code?, version, result, at}`), written in the
  same transaction as the state; Redis `idem:{user_id}:{key}` TTL 600 s is only a fast-path cache.
  Rejected commands are also recorded (a replay returns the same rejection).
- **buildEnd:** auth + rate limit, read version without lock, 200 `{ok:true, version, result:{}, events:[], snapshot:null, replayed:false}`; no write, no idempotency.
- **tick / undo / redo:** since S11 `tick` is not in the `Command` union: the API answers 422 `validation_error` and never calls the sim.
  Since S12 undo/redo are served by the sim from `state.history` (ordinary effect commands, version + 1, idempotent); with nothing to
  undo/redo the core says "Нечего отменять/повторять" -> 422 `command_rejected`, `details.reason = "rejected"`.
- **GET /api/bootstrap:** creates factory on first call (INSERT .. ON CONFLICT DO NOTHING, then SELECT .. FOR UPDATE), ensures an
  open season row exists (creates season 1 if none; rotation is S4), catch-up with 8 h cap, returns
  `{version, serverTime, season:{id, startsAt, endsAt}, features:{undo, serverOrderEta, ..}, snapshot}`; since S12 `undo` = sim
  `/health.historyInState` = true and `serverOrderEta` = true (`Order.etaAt` is set by the core on every loading order whose line can finish it).
- **GET /api/factory/me:** catch-up under lock, `{version, serverTime, snapshot, degraded:false}`; sim down -> last stored snapshot
  (`factories.snapshot` JSONB column) with `degraded:true` (200).
- **POST /api/advance** body `{cap_hours: 0..8 (default 8), reason: "resume"|"manual"|"background"}` -> `{version, serverTime, advancedMs, snapshot}`.
- **Shift limit:** the counter lives in the factory state (top-level `state.shiftsLeft` / `shiftsDay` (MSK day) since schema v2, before S12
  `state.season.shiftsLeft/shiftsDay` — part of factories.state, decremented inside the same sim command and the same DB transaction). No Redis counter. Server season
  bounds (seasons table) are aligned in S4.
- Denormalised columns (credits, stars, level, xp, bottles_season, quality_avg, last_tick_at, updated_at) and `snapshot` are
  refreshed from the sim snapshot on every write.
- **Sim (S2, kept in S12):** rejections carry `result.code` (core `r.code` first, fallback table `sim/src/reasons.ts`); `/v1/advance` accepts
  `reason`, and `background` accumulates into an unseen offline report instead of replacing it (the core replaces, so the wrapper in
  `sim/src/engine.ts` merges); the sim itself enforces `effectiveNow = max(now, state.lastTick)` on every endpoint.

## S3 decisions

- submitShift is validated in the API (`api/app/shift.py`) before the sim call; the sim keeps its own checks as a second line.
  Top-level code `minigame_too_short` or `impossible_score`; `details.reason` is the rule name (e.g. `score_out_of_range`,
  `duration_too_long`, `win_score_too_low`). Accepted shifts are logged in table `shifts` in the same transaction.
- `features.undo` = sim `/health.historyInState` (true since S12), cached 60 s.
- States over 150 KB are logged with `state_bytes`; the 150 KB audit test (`api/tests/test_game_sim.py`, opt-in `SIM_URL`) is a plain test
  since S12 (158 commands on the 48-building demo layout: ~31 KB).

## S4 decisions (part A: leaderboard, visit, summary, bots)

- **Source of truth:** denormalised `factories` columns (`stars`, `quality_avg`, `speed_bph`, `university`, `is_bot`; migration 0004), refreshed
  from the sim snapshot on every write. `quality_avg` is 0 until something is produced (the sim reports 1 for an empty factory).
  `speed_bph` is derived (the snapshot has no flow rate): `snapshot.factory.bottlesPerHour` if a sim ever provides it, else
  d(produced)/d(time) between a baseline kept in `state.meta.speed` {t,p} and now (baseline moves after >= 60 s, window capped at 8 h);
  idle/new factories show 0. S12: `snapshotOf` of lib/core does not expose `bottlesPerHour` either (it stays inside the core's own speed
  board), so the delta stays; if the frontend ever adds it to `Factory`, the first branch picks it up with no API change. `users.university` follows `setUniversity`.
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

## Round 3 coordination (S4 part B, S5, S7-early run in parallel)

- **Background jobs** run only in the bot process (APScheduler), but the bot never touches factory state itself: it calls
  internal API endpoints `POST /internal/...` protected by header `X-Internal-Token: $INTERNAL_TOKEN` (constant-time compare;
  missing/wrong -> 401 unauthorized; the internal router is not exposed by Caddy — Caddy proxies only /api/*, /health, /ready).
  The bot may read the DB directly for analytics/lead statuses (S6).
- **Alembic chain:** 0004 (exists) -> `0005_seasons` (S4 part B) -> `0006_leads_quiz_events` (S5) -> later ones. Revision ids
  are exactly these strings.
- Shared files `api/app/models.py` and `api/app/main.py`: only append new classes / router includes with small Edit calls,
  re-read right before editing (another worker may have changed it).
- **Leads (S5):** table `leads` (user_id, season_id, full_name, birth_date, phone/email/telegram (at least one), university,
  direction, course, vacancy (profession id), ready_from/readiness, practice_type, consent bool, consent_at,
  consent_text_version, consent_revoked_at, tags text[], status new|contacted|interview|rejected|hired, hr_note, best_profession,
  season_place, quiz_score, created_at, updated_at; unique (user_id, season_id)). Table `outbox` (id, kind, payload jsonb,
  created_at, sent_at, attempts, last_error) — the API inserts `lead_created` in the same transaction as the lead; the bot (S6)
  delivers it. Tables `quiz_answers` (user_id, question_id, correct, answer, answered_at, pk(user_id, question_id)) and
  `events` (id, user_id, name, props jsonb, ts, source, received_at; indexes (name, ts), (user_id, ts)).
- **Bots (is_bot users)** are excluded from funnel/export/sources/HR analytics everywhere.

## S7 decisions (stabilisation, early part)

- **Circuit breaker** (`sim_client.CircuitBreaker`, per process): `SIM_BREAKER_THRESHOLD` (5) consecutive sim failures (timeout, connection
  error, 5xx, invalid body; a sim 4xx does not count) open it for `SIM_BREAKER_OPEN_S` (10 s): every sim call fails fast with the usual
  503 `sim_unavailable` (`details.reason = "circuit_open"`, `Retry-After`) without touching the network; then exactly one half-open probe.
  Transitions are logged (`breaker_state`, `consecutive_failures`, `opened_total`); the access log carries `breaker_state` while it is not closed.
  `factory/me` keeps its degraded path (it catches `sim_unavailable`), leads / quiz / events / leaderboard never call the sim.
- **Timeouts:** DB `statement_timeout` 3 s + `lock_timeout` 2 s as asyncpg `server_settings` (`DB_STATEMENT_TIMEOUT_MS`, `DB_LOCK_TIMEOUT_MS`;
  not for migrations); with `PGBOUNCER=1` (no startup parameters in transaction pooling) the same 3 s is asyncpg `command_timeout` plus
  pgbouncer `QUERY_TIMEOUT=4`. Both surface as 503 `db_unavailable` with `details.reason` `statement_timeout` / `lock_timeout` (other DB errors stay 500).
  Whole request `REQUEST_TIMEOUT_S` (5): the middleware cancels the handler and answers 503 `timeout` (`Retry-After: 1`, `details.timeout_s`);
  paths under `/internal/` are exempt (`middleware.NO_TIMEOUT_PREFIXES`). Requests slower than `SLOW_REQUEST_MS` (1000) log
  `"slow request"` at WARNING with `duration_ms` and `slow: true`.
- **INTERNAL_TOKEN** (api + bot; `API_INTERNAL_URL=http://api:8000` for the bot): `APP_ENV=prod` needs >= 16 chars and not a placeholder -
  otherwise the api raises at startup (lifespan) and `/ready` lists `INTERNAL_TOKEN` as missing (`config_error`). Caddy answers 404 for `/internal/*`.
- **Compose:** `API_WORKERS` -> uvicorn `WEB_CONCURRENCY` (default 2, keep <= 4), `mem_limit`s, profile `backup` (`scripts/backup.sh` loop,
  `scripts/restore.sh`), `restart: unless-stopped` on every long-running service. E2E: `e2e/test_s7_resilience.py` (`E2E_DOCKER=1`).

## S4 decisions (part B: season rotation, internal jobs)

- **Rotation** (`api/app/seasons.py`): a season closes when `now >= ends_at` (`closed`, `closed_at`; final standings frozen into
  `season_results` (season_id, user_id, place, stars, quality, speed_bph, bottles, is_bot; migration 0005): stars desc, user id asc, bots only while
  BOTS_ENABLED). The next season opens when `now >= previous ends_at + 1 h` (SEASON_ROLL_H, = the core's constant), starts at the moment it is
  opened and lasts SEASON_DAYS = 7. The 1 h counts from `ends_at`, not from when the job noticed (so a late job opens the next season at once).
  `rotate()` is idempotent and race-safe: cheap pre-check without a lock, then `pg_advisory_xact_lock` + re-read. It never locks factory rows, so
  it may run inside a factory write transaction (no lock cycles). Factories are NOT bulk-updated: a factory moves to the new season lazily, the
  next time it is loaded under its row lock (`factories.season_id` and the denormalised columns follow from that write). Until then it still
  shows in the old (closed) season; the frozen standings are in `season_results`. Known limits: the leaderboard of a CLOSED season is still
  computed live from `factories` (members leave it as they move on) and production of a stale factory between its last visit and `ends_at` is
  credited to the new season when it comes back (the state already carries the new season when the core's catch-up runs).
- **State alignment** (`seasons.align_state`, called by `GameService._lock` on every locked load: bootstrap, command, advance, factory/me,
  background): `state.season.{id,startsAt,endsAt}` := the server's newest season BEFORE the sim call; when the id differs the core's season reset runs
  (`state.sim.stars = 0`, `shipped = {}`, `rejected = 0`; factory, credits, level, xp, collection stay; `shiftsLeft` / `shiftsDay` untouched). A fresh
  factory is aligned after the sim created it and `snapshot.season` is patched in place (id / bounds / closed / dayIndex), no second sim call.
  **Why the core's own `rollSeason` never fires:** it runs in every catch-up (bootstrap, advance, command) when `now - state.season.endsAt >= 1 h`. The state's season is always the
  server's newest one, and the API rotates inline (`seasons.current`) whenever `now >= newest.ends_at + 1 h`, so the state's season is open or closed
  for less than 1 h at every sim call. The sim's `seasonOpen = now < season.endsAt` therefore agrees with the server; during the 1 h gap the
  snapshot says `season.closed = true` with the old id and bootstrap returns that closed season.
- **Internal endpoints** (`api/app/routes/internal.py`, `X-Internal-Token`, 401 `unauthorized` when missing / wrong / `INTERNAL_TOKEN` unset;
  no rate limit, no request timeout, Caddy answers 404):
  `POST /internal/seasons/rotate` -> `{closed:[ids], opened:id|null}` (a newly opened season also gets the bots right away);
  `POST /internal/top/advance {limit:200 (1..1000)}` -> `{advanced, skipped, failed, ms, sim_down, pending, season_id, season_open}`: the top-N non-bot
  players of the newest season by stars, one by one, each in its own short transaction (`SELECT .. FOR UPDATE SKIP LOCKED`, locked = `skipped`),
  `reason=background`, cap 8 h, no version check, no version bump; `sim_unavailable` stops the run (`sim_down`, `pending` = not tried),
  another error counts as `failed` and the run goes on; nothing to do while the newest season is over;
  `POST /internal/bots/grow` -> `{bots, season_id, progress}` (`seed_bots.grow_current`: real season progress, monotonic inside a season for
  stars / speed / quality / credits / xp / bottles, takes the season advisory lock, no-op `skipped: no_open_season` during the gap).
- **Bot jobs** (`bot/app/jobs.py`, `API_INTERNAL_URL` default http://api:8000, `INTERNAL_TOKEN`): `seasons_rotate` every 1 min, `top_advance` every
  12 min (+-90 s jitter, body `{limit:200}`), `bots_grow` every 10 min (+-30 s jitter); `max_instances=1`, `coalesce=True`; every job logs and
  swallows errors (HTTP error, timeout, refused connection, missing token). aiohttp client (already an aiogram dependency).

## S5 decisions (leads, quiz, events)

Everything below is DB + Redis only: no sim call anywhere (tests run with a sim that is down and counts attempts; e2e stops the sim container).
Alembic `0006` (down_revision `0005`: the revision ids are the short strings `0001`..`0006`, the file names carry the words).

- **POST /api/leads** (limit `leads` 5/min, 201, body camelCase or snake_case): required `fullName` (2..120), `university`, >= 1 contact, `consent: true`
  + `consentTextVersion`. Contacts: `phone` / `email` / `telegram` and/or the legacy `contact` (classified by format: e-mail, phone -> `+7...`,
  telegram username; stored bare, without `@`). Optional case fields: `birthDate` (YYYY-MM-DD), `direction` (free text), `course` ("4", 4, "Выпускник"),
  `vacancy` or legacy `roleId` (one of the 7 profession ids), `readyFrom` (date), `readiness` (`now|season|later`), `practiceType` (`paid|free|job`) -
  optional because the vendored form does not send them yet. Check order: 401 -> 429 -> 422 (shape and fields) -> 403 `consent_required` -> 403
  `age_required` -> 409 `lead_already_exists` (`details.revoked`). Age: `birthDate` < 18 -> 403 even if the age was confirmed; no `birthDate` ->
  needs `users.age_confirmed` or `snapshot.player.ageConfirmed` (the real `ageConfirm` lives in the sim state; `users.age_confirmed` is not synced
  from it yet), else 403. Season = `seasons.ensure` (newest season, same as factory writes); one lead per (user, season) via `ON CONFLICT DO NOTHING`.
- **Tags** (`leads.tags`): `senior` (course >= 4 or graduate), `profile_direction` (keyword match of `direction` against
  `leads.PROFILE_DIRECTIONS`: food tech, chemistry, biotech, mechanics, automation, electrical, logistics, quality), `ready_for_season`
  (`readiness` now|season, or `readyFrom` within 120 days), `high_quiz` (>= 8 of 10 correct). Enrichment from existing data, else null:
  `best_profession` (stored snapshot skills), `season_place` (general rank), `quiz_score` (quiz_answers, null until the first answer). A later quiz
  answer batch refreshes `quiz_score` + `high_quiz` of the player's non-revoked leads.
- **Outbox**: `lead_created` / `lead_revoked` rows (`payload` snake_case; lead_created carries the card fields: lead_id, user_id, tg_id, source,
  full_name, university, direction, course, vacancy, practice_type, readiness, phone, email, telegram, tags, quiz_score, best_profession,
  season_place, created_at) are inserted in the SAME transaction as the lead / revocation; delivery and `sent_at` are the bot's (S6).
- **GET /api/leads/me**: latest lead (owner's view, no `hr_note`), 404 `lead_not_found`. **DELETE /api/leads/me**: `consent_revoked_at = now` on every
  non-revoked lead of the player, status untouched, 204; repeating it is a no-op 204 (no second outbox row); 404 without any lead. A revoked lead
  still blocks a new one in the same season (409, `details.revoked`). Bot/analytics must exclude `consent_revoked_at IS NOT NULL` from the active
  funnel and mailings. Season summary: `leadStatus` = latest lead status, `"revoked"` after revocation, else null.
- **Personal data in logs**: access log has no bodies/queries; engine `hide_parameters=True` (no SQL parameters in DBAPI error messages);
  validation errors carry field paths and messages only, never input values.
- **Quiz**: bank in `api/app/quiz_bank.py` (10 questions from the museum facts; correct index only there). `GET /api/quiz/questions` ->
  `{questions:[{id,text,options}], progress:{score,answered,total,completed,answeredIds}}`. `POST /api/quiz/answer` body `[{questionId, answer}]` or
  `{answers:[...]}` (answer = option index; <= 10 items) -> `{results:[{questionId, correct, correctAnswer, explanation}], score, answered, total,
  completed}`. All-or-nothing; 422 `unknown_question` | `invalid_answer_format` | `validation_error` (shape, duplicates), 409
  `quiz_already_completed` for a re-answered question or a finished quiz (answers are final); limit `quiz` 30/min (GET and POST share it).
  Summary `quizResult` = `{score, answered, total, completed}` or null.
- **POST /api/events** (limit `events` 10 batches/min): body `{events:[{name, props?, ts}]}` (or a bare list), `ts` = ms since epoch or ISO string
  (missing = now). All-or-nothing: > 50 -> 422 `too_many_events`, name outside `events.EVENT_NAMES` (= analytics.ts `EventName`, drift test) ->
  `invalid_event_name`, `ts` > now + 60 s -> `future_timestamp`, props > 2 KB / bad types / body NUL -> `validation_error`, body > 256 KB -> 413.
  Response `{accepted:n}`; `source` = `users.source`; `users.is_bot` batches are validated and dropped (`accepted: 0`).
- **Client** (`clients/web/HttpGameApi.ts`): `submitLead` (adds `consentTextVersion`, 409 counts as ok, `lastLeadError`), `myLead`, `revokeLead`,
  `quizQuestions`, `quizAnswer`, `track` (clamps `ts` to the server clock, drops 4xx batches, rethrows network/429/5xx so analytics.ts re-queues).

## S6 decisions

- HR bot (`bot/app/hr/`): access by `HR_ADMIN_TG_IDS`; lead cards to `HR_CHAT_ID` or every admin; outbox job every 7 s
  (`FOR UPDATE SKIP LOCKED`, backoff, never consumed without BOT_TOKEN); status buttons append an audit line to `hr_note`;
  /leads /lead /export (CSV `;` + BOM) /funnel /sources /stats /top /hr. Demo bots excluded from all analytics.
- Player features (`bot/app/player/`, migration 0007): /start upserts the user (first-touch source), reminder opt-in toggle,
  /notify /stop; order reminders every 5 min from `factories.snapshot` (completion field optional until S1: `etaAt`/`completesAt`,
  else deadline), quiet hours 23–09 MSK, dedupe table `reminders_sent`; 5xx alert from Redis per-minute counters
  (`metrics:req:*`, `metrics:5xx:*`), ≥20 requests and >20 % 5xx over 10 min, 15 min cooldown.
- `users.age_confirmed` follows `snapshot.player.ageConfirmed` in the same transaction as the command.

## S9 decisions (lead form and quiz in the client's shape)

The client (`vendor/frontend`, `docs/http-contract.md` rev 4, `web/src/lib/core/http/fakeApi.ts`) is the source of truth; the old names stay accepted.
Alembic `0008` (down_revision `0007`): `leads.institution_kind` (`vuz|college`, check constraint, NULL for older leads).

- **POST /api/leads** accepts the client's `LeadForm` as is: `tgUsername` (= `telegram`; the older name wins when both are sent), `consentVersion`
  (= `consentTextVersion`), `department` id, `institutionKind`, `roleId` (= `vacancy`), `birthDate/phone/email/readyFrom/practiceType`; snake_case too.
  Telegram usernames are 4..32 chars (the contract's own example is `ivan`; Telegram says 5, Fragment sells 4).
- **department** `lab|bottling|tech|warehouse|engineering|other` is stored as the human label in `leads.direction` (Лаборатория, Розлив, Техника и
  наладка, Склад и логистика, Инженерия, Другое) and gives `profile_direction` by id (`leads.DEPARTMENTS` -> `PROFILE_DIRECTIONS` key; `other` gives no
  tag). It wins over the free-text `direction`, which still works alone (keyword match). Unknown id -> 422 `validation_error`. Same for `institutionKind`.
- **senior** tag: course >= 4, a graduate, or `institutionKind == college` (any course). **high_quiz**: `HIGH_SCORE` 0.75 of the bank = 6 of 8 correct.
- **GET /api/leads/me** (and the POST body) = the owner's view with `createdAt` as ms epoch (number, the client's `LeadInfo`), `roleId` (= `vacancy`),
  `status`, plus all the other fields (`institutionKind` added; `consentAt`, `updatedAt` stay ISO strings).
- **One quiz bank**: the frontend's (`vendor/frontend/web/src/lib/api/mock/quiz.ts`, 8 questions `q_year`...`q_space`, options `a|b|c`). The api image
  is built from `./api`, so the bank is generated into `api/app/data/quiz_bank.json` by `python scripts/gen_quiz_bank.py` (strict parser of the TS file,
  `--check` for CI); `api/tests/test_quiz_bank_drift.py` fails when the vendored TS moves and the JSON does not (also: department labels, enums,
  the bot's copy of the ids). After a submodule bump run the script and commit the JSON.
- **POST /api/quiz/answer**: `{answers:[{question_id|questionId, answer}]}` or a bare list; `answer` = option id (`a|b|c`, case-free) or the 0-based
  index. Response `{correct, total, score 0..100, results:[{question_id, questionId, correct, correctAnswer}], answered, completed, bankTotal}`:
  `total` is the number of questions answered so far (the client's `total`), `score = round(100 * correct / total)` halves up like `Math.round`.
  Questions already answered are skipped in a batch (first answer stays, not re-scored, like fakeApi); a batch with nothing new -> 409
  `quiz_already_completed` (`details.completed` says whether the bank is done). 422 `unknown_question | invalid_answer_format | validation_error`.
  GET /api/quiz/questions: `options` texts + `optionIds`, `progress` has the same scalars + `answeredIds`. Summary `quizResult` = the same scalars or null.
- Rows of the S5 bank (`q1978`...) stay in `quiz_answers` but are never counted (API: `quiz_counts`/progress filter by the bank ids; bot `/stats` filters
  by `format.QUIZ_QUESTION_IDS`). `leads.quiz_score` is still the number of correct answers (the bot prints it as `x/8`).
- **HR bot**: the card shows `🎓 university · вуз|колледж · курс: N` and `🧭 Направление: <department label>`; CSV has `вид учреждения`; quiz total is 8.


## S11 / S12 decisions (vendored game core, `lib/core`)

- **Contract (S11):** `vendor/frontend` is pinned to frontend `main` (`422b6fc`); `contract/generate.mjs` tolerates a missing / non-union
  `SimEvent`. `Command` has 27 variants (`tick` is gone: API 422 `validation_error`, sim 400 unknown command), `Order.etaAt?`,
  `GameSnapshot.version?`, `CommandResult.{version,replayed,code}?` are generated. `LeadForm` is not part of the generated union (leads are
  validated by the API, S9). The `sim` image is tagged `mosprom-sim:<vendor sha>` (`SIM_COMMIT=$(scripts/simcommit.sh)`).
- **Sim engine (S12):** `sim/src/engine.ts` calls the pure functions of `$lib/core` (`migrateState`, `freshState`, `advanceState`,
  `applyCommand`, `snapshotOf`) - the same ones the frontend's reference server runs (`core/http/fakeApi.ts`), so client and server count
  bit for bit. No `MockGameApi`, no `localStorage` shim, no `Date.now` patching, no mutex: every call works on a `structuredClone` of the
  request state (the caller's object is never touched), time is a parameter. HTTP API of the sim is unchanged (`/v1/*`).
- **State shape (schema `v: 2`):** top-level `shiftsLeft` / `shiftsDay` (were in `season`), `history: {undo[], redo[]}` (compact inverse
  diffs of the layout, max 20 steps; was memory-only in the mock), `season: {id, startsAt, endsAt}`, no `prodAt` / `discReal`; `layoutV: 2`.
  The API only reads `lastTick`, `season.{id,startsAt,endsAt}` (alignment), `sim.layout`, `sim.stars|shipped|rejected` (season reset),
  `player.{university,ageConfirmed}` and `meta`, all unchanged in v2; `seasons.align_state` therefore works on v1 and v2 alike.
- **Migration:** lazy and in place, on every load: `loadState` = `structuredClone` + `migrateState` (v1 -> v2: shift counter moved to the root,
  `history` added, 12x10 hangar upgraded) + dropping the mock-only `clock` / `lastSeenReal`. Nothing is migrated in bulk: the migrated state is
  persisted by the next write (any bootstrap / factory/me / advance / command; `/v1/snapshot` reads migrate on a copy). A state with `v` newer
  than the core (`STATE_V`) is a sim 400 `bad_request`. Unknown top-level fields (`meta.idempotency`, `meta.speed`, ...) are kept as is.
  Tested on real states dumped from the dev DB (`sim/test/fixtures/v1-states.json`) and on a factory played over HTTP on the old sim
  (`v1-played.json`, snapshot parity with what the old sim returned).
- **Time:** `now` is a parameter; every entry point uses `max(now, state.lastTick)`. A command arriving after a gap beyond the 8 h cap first
  runs the capped catch-up *with* the offline report (the core's `applyCommand` catches up silently).
- **Offline report:** the core builds it on `advanceState(.., {offlineReport: true})` and replaces the previous one. `background` advances
  keep accumulating into an unseen report (`mergeOffline`, wrapper in `engine.ts`); the other reasons replace it.
- **Rejection codes:** `r.code` of the core when present (today `daily_limit_exceeded`); `submitShift` plausibility (`minigame_too_short`,
  `impossible_score`) is decided in the engine before the core, in the order of the reference server; the remaining machine reasons
  (`season_closed`, `insufficient_funds`, `not_found`) come from the message table in `sim/src/reasons.ts` until the core codes them
  itself (a drift test scans the vendored lib for new messages). Everything else is `rejected`.
- **Bots:** lib/core has its own deterministic bots (`core/bots.ts`, 1300, only used by `leaderboardOf` / `visitOf` when no board is given).
  The server keeps its own `seed_bots` (rows in `users` / `factories`, ranked by SQL together with real players, with stored layouts),
  because the leaderboard, visits, season results and analytics are DB-backed; the sim passes no board to `snapshotOf` (the API sets
  `snapshot.rank` itself), so the core's generated bots never reach a response.
- **`etaAt`:** `scheduleOrders` runs at the end of every `advanceState` / `applyCommand`: every loading order gets `etaAt` while its line
  can finish it within 48 h; a rebuild / upgrade / break shifts it, a cut line removes it, undo brings it back.
- **State size:** ~31 KB after 158 commands (incl. a full 20-step undo history and the 50-entry idempotency log) on the 48-building demo
  layout; the 150 KB audit is a regular test.
