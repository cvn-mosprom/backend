# Progress

Resume rule: read this file, docs/ARCHITECTURE.md and docs/spec-rev4.txt, then continue from "Next".

## Done
- Repo scaffold, frontend pinned as submodule vendor/frontend (dde6cbf).
- S0 done: compose (caddy api db migrate redis sim bot + pgbouncer/sim-replicas profiles), health/ready, initData + dev auth, errors, request id, JSON logs, Redis rate limiter, generated contract (28 cmds), sim service. Tests: api 125, sim 55, contract 212, bot 19.
- S2 done: GET /api/bootstrap, POST /api/command (row lock, 409, idempotency deque in state.meta + Redis), POST /api/advance, GET /api/factory/me (degraded), buildEnd no-op, sim reason codes, shift limit in state + max(now,lastTick) guard, background offline report accumulation, clients/web/HttpGameApi.ts, e2e S2 acceptance (9/9). Tests: api 257 (with SIM_URL), sim 86, contract 212, e2e 9.
- S3 done: submitShift rules (api/app/shift.py), shifts log table, features.undo from sim health, 150 KB state warning + audit test (24 KB today). Tests: api 396, sim 87, e2e 9.
- S4 part A done: leaderboard (tabs, Redis 20 s cache, me/gapTo51), factory/{playerId}, season/summary, real snapshot.rank, bot seed (`docker compose exec api python -m app.seed_bots`), speed_bph measured from produced delta. Tests: api 434, e2e 18.

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
- S4 part B: server season rotation (close by ends_at, results, new season +1 h; align state.season of the mock), background top-200 catch-up every 10–15 min (bot APScheduler, reason=background, no version bump), bot growth job (seed_bots.grow_bots).
- Then S5 (leads, 18+, consent, quiz, events), S6 (HR bot), S7 (stabilisation).
