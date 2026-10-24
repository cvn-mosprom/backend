# Progress

Resume rule: read this file, docs/ARCHITECTURE.md and docs/spec-rev4.txt, then continue from "Next".

## Done
- Repo scaffold, frontend pinned as submodule vendor/frontend (dde6cbf).
- S0 done: compose (caddy api db migrate redis sim bot + pgbouncer/sim-replicas profiles), health/ready, initData + dev auth, errors, request id, JSON logs, Redis rate limiter, generated contract (28 cmds), sim service. Tests: api 125, sim 55, contract 212, bot 19.
- S2 done: GET /api/bootstrap, POST /api/command (row lock, 409, idempotency deque in state.meta + Redis), POST /api/advance, GET /api/factory/me (degraded), buildEnd no-op, sim reason codes, shift limit in state + max(now,lastTick) guard, background offline report accumulation, e2e S2 acceptance (9/9). Tests: api 257 (with SIM_URL), sim 86, contract 212, e2e 9.
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
- Integration S8 done: X-Telegram-Init-Data / X-Dev-User auth, CORS (CORS_ORIGINS), advance heartbeat/order_due, 409 details.version, ?start_param=, features quiz/leads, top-level toast/rewards/snapshot.
- Integration S9 done: client LeadForm shape (aliases, department ids, institution_kind, migration 0008), LeadInfo with ms createdAt, one quiz bank generated from vendor quiz.ts (scripts/gen_quiz_bank.py --check), client quiz response.
- Integration S11+S12 done: submodule at frontend main 422b6fc, contract regenerated (27 commands, tick -> 422), sim on $lib/core (migrateState v1->v2, undo/redo live, etaAt, features.undo/serverOrderEta true), 150 KB audit enforced (31 KB).
- Integration S10 done: e2e/client runs the REAL vendor HttpGameApi against the live stack (journey + retries, vitest; wrapped by e2e/test_s10_client.py), raw transport test e2e/test_s10_transport.py (preflight, CORS on 200/4xx/429/5xx, both auth headers); clients/web removed, ARCHITECTURE points to vendor/frontend/docs/http-contract.md.
- Integration S13 done: caddy image = web build (web/Dockerfile, VITE_API_URL=/) + proxy on one origin (SPA fallback, /internal 404, WEBAPP_URL=https://$DOMAIN/); POST /api/events partial accept ({accepted, rejected}, limit 10+burst 6); users.is_test / factories.is_test (migration 0009; dev name `e2e*` or tg_id >= 10^12) excluded from top, ranks, season results, HR queries, reminders, lead cards; `python -m app.purge_test_users`; README/ARCHITECTURE refreshed, S1/S1U no longer pending.
- Tests now: api 808 (SIM_URL), sim 103, contract 200, bot 104, e2e 245 (+6 skipped).

## Next
- Live Telegram demo run on a real domain with BOT_TOKEN (manual checklist in README "Demo in Telegram"); run `python -m app.purge_test_users` first.

## Open after S10/S13 (small)
- DONE (S14): GET /api/leads/me -> 404 after DELETE and a new lead may be submitted (migration 0010, partial unique index); the it.fails in journey.test.ts is a plain it().
- DONE (S14): e2e/client newDevUser() ids >= 10^12 -> flagged is_test.
- Dev DB has ~1770 old 'smoke' users not flagged: `docker compose down -v` or flag + purge before the demo.

## S14 (web login) and fixes — done
- Revoked lead: GET /api/leads/me -> 404, resubmit allowed (partial unique index, migration 0010); e2e/client ids >= 10^12 (is_test).
- Web login via the bot (migration 0011): /login code -> POST /api/auth/web/code; deep link t.me/<bot>?start=weblogin_<nonce> + /api/auth/web/start|poll; Authorization: Bearer sessions (30 d sliding); /logout_web. One account = one tg_id = one factory. Frontend contract: docs/web-auth.md (frontend lead must add the login screen + Bearer header).
- Local bot container kept STOPPED (real BOT_TOKEN in .env; the lead runs the bot elsewhere). Rebuild with `--no-deps api`.
- Tests: api 848, bot 125, e2e 255 + 5 skipped.

## S15 media in SeaweedFS — done
- seaweedfs (S3 :8333, bucket `media`, anonymous read only), one-shot media-sync (scripts/media_sync.py) uploads vendor/frontend/web/static/{landing,labels,audio,icons}; Caddy proxies those paths to SeaweedFS; web image no longer carries them. Re-run `docker compose run --rm media-sync` after a submodule bump.
- Tests: api 867, e2e 360 + 6 skipped.

## HR CRM (docs/hr-crm-plan.md)
- C1 done: 38 achievements, pure engine, hooks after command/shift (savepoint), internal recompute + bot job every 10 min, backfill (dev: 114 granted to 49 players). Migration 0012.
- C3 done: hr_users roles (analyst < hr < hr_lead < admin), lead_events, /api/hr/me, /api/hr/users, PII masking for analyst, bot uses roles. Migration 0013.
- Tests: api 1054, bot 146, e2e 360 + 6 skipped.
- Next: C2 (points/competencies/fit, player API, achievementsUnlocked, outbox notifications gold+seasonal) and C4 (candidates, pipeline, notes, messages) in parallel; then C5, C6.
- C2 done: competencies + fit (HR only), /api/achievements/me|catalog, achievementsUnlocked, leaderboard tab achievements, ОД in summary/visit, outbox + bot notify gold/seasonal, lead_events achievement (migration 0015).
- C4 done: /api/hr/candidates list/card/PATCH (version), notes, messages via bot (409 bot_not_started), pipeline, new statuses (migration 0016). Frontend HttpHrApi live tests pass.
- C5 done: vacancies + matching, analytics, export.csv, ОД in bot (migration 0017).
- Tests: api 1267, bot 177, e2e 372 + 6 skipped. C6 skipped for now (optional). Bot image must be rebuilt before the bot is started again.
