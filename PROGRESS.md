# Progress

Resume rule: read this file, docs/ARCHITECTURE.md and docs/spec-rev4.txt, then continue from "Next".

## Done
- Repo scaffold, frontend pinned as submodule vendor/frontend (dde6cbf).
- S0 done: compose (caddy api db migrate redis sim bot + pgbouncer/sim-replicas profiles), health/ready, initData + dev auth, errors, request id, JSON logs, Redis rate limiter, generated contract (28 cmds), sim service. Tests: api 125, sim 55, contract 212, bot 19.
- S2 done: GET /api/bootstrap, POST /api/command (row lock, 409, idempotency deque in state.meta + Redis), POST /api/advance, GET /api/factory/me (degraded), buildEnd no-op, sim reason codes, shift limit in state + max(now,lastTick) guard, background offline report accumulation, clients/web/HttpGameApi.ts, e2e S2 acceptance (9/9). Tests: api 257 (with SIM_URL), sim 86, contract 212, e2e 9.
- S3 done: submitShift rules (api/app/shift.py), shifts log table, features.undo from sim health, 150 KB state warning + audit test (24 KB today). Tests: api 396, sim 87, e2e 9.
- S4 part A done: leaderboard (tabs, Redis 20 s cache, me/gapTo51), factory/{playerId}, season/summary, real snapshot.rank, bot seed (`docker compose exec api python -m app.seed_bots`), speed_bph measured from produced delta. Tests: api 434, e2e 18.
- S4 part B done: server season rotation + season_results, state.season alignment, /internal/* (X-Internal-Token), bot APScheduler jobs (rotate 1 min, top-200 advance 12 min, bots grow 10 min).
- S5 done: leads (18+, consent, one per season, tags, outbox lead_created/lead_revoked), DELETE leads/me (152-FZ), quiz (10 server-side questions), events batches with whitelist. Works with sim down.
- S7 early done: sim circuit breaker, DB 3 s / lock 2 s / request 5 s timeouts, slow log, backup profile + scripts, restart resilience e2e, README for jury.
- S6 done: HR bot (outbox delivery, lead cards + status buttons, /leads /export /funnel /sources /stats /top), reminders opt-in + order reminders, 5xx alert, ageConfirm -> users.age_confirmed.
- S7 smoke suite done (e2e/test_s7_smoke.py, 36 scenarios / ~172 items). It found 5 real 500s, kept as xfail: NUL in idempotency_key, NUL in renameFactory name, leaderboard ?university=%00, factory/<5000 digits>, dev auth tg_id > bigint.
- Smoke-found 500s fixed (NUL in strings/keys -> 422, bounded player id -> 404, tg_id > bigint -> 401, DB 22021/22P05 -> 422).
- Tests now: api 662, sim 87, contract 212, bot 97, smoke 171 (0 xfail).

## In progress
- (none)

## Notes for later sprints (from sim worker)
- API must send `now = max(now, state.lastTick)` (rollDay compares MSK days with !==; time going back could re-grant shifts).
- Sim 413 payload_too_large -> API should map to 500/422; sim 400 on schema-invalid cmd -> API must validate cmd first.
- undo/redo/tick rejected in sim/src/engine.ts REJECTED_COMMANDS (remove undo/redo there after S1U).
- Background advance (S4 top-200) overwrites unseen `offline` report every run -> pass reason and keep/accumulate the first report.
- Mock season roll runs inside bootstrap/advance; S4 server-side rotation must override/align season fields in state.
- Command after >8 h gap: sim runs capped bootstrap first, then the command.

## Next
- All planned sprints done. Waiting for the lead: S1 (order ETA in snapshot -> make field required, regenerate contract via contract/generate.sh) and S1U (undo diffs -> remove undo/redo from sim REJECTED_COMMANDS). Frontend: plug clients/web/HttpGameApi.ts.
- Migration ids are short: next is "0007" with down_revision "0006".
