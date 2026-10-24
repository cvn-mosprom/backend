# e2e: S2 acceptance against the running compose stack

```
cd e2e
py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # once
.venv/Scripts/python -m pytest                                                      # stack must be up
```

Env: `E2E_BASE` (default `https://localhost`, TLS verification off), `E2E_ORDER_WAIT_S` (default 120; `0` = stop
after "order accepted, loading started"). Auth is `Authorization: dev <random tg id>` (needs `DEV_AUTH=1`); every
test gets a fresh user, so tests are independent and need no cleanup.

| Test | S2 "Готово когда" |
|---|---|
| `test_a_tutorial_passes_on_server` | tutorial passes on the server (full line via commands, accept order, wait for shipment ~30 s real time) |
| `test_b_reload_does_not_lose_factory` | reload does not lose the factory |
| `test_c_two_tabs_passive_sync_never_409` | two tabs on passive sync do not fight |
| `test_d_stale_version_409_resync_retry` | stale action -> 409 -> factory/me -> retry |
| `test_e_replay_same_key_does_not_charge_twice` | same idempotency key does not charge twice |
| `test_f_sixth_shift_of_the_day_rejected`, `test_f2_...` | 6th shift rejected (`daily_limit_exceeded`), plausibility codes |
| `test_g_build_end_does_not_change_version` | buildEnd no-op |
| `test_h_undo_redo_tick_rejected` | undo/redo -> 422 `history_unavailable`, tick -> `tick_removed` |

`tutorial.py` holds the exact command sequence (derived from `content/tutorial.ts`, `sim/templates.ts tutorialLayout`,
`content/dialogs.ts` quest ids) and was verified against the sim service directly.

## S4 (leaderboard) - `test_s4_leaderboard.py`

Seeds the demo bots once per session with `docker compose exec -T api python -m app.seed_bots` (run from the repo root; the tests skip when that
fails), then checks all four tabs (rows, `me`, `gapTo51`, `universities`), `limit` / university filter / cache, the error paths, the public
`GET /api/factory/{id}` view of a bot and of a real player, `snapshot.rank` == leaderboard place, and the `GET /api/season/summary` shape.

## S7 (resilience) - `test_s7_resilience.py`

Skipped unless `E2E_DOCKER=1`: it runs `docker compose restart api sim db` and `docker compose stop/start sim` in the repo root, so
nobody else should use the stack meanwhile. Player `Authorization: dev 777001:s7` (fixed, so a re-run reuses the same factory).
* `test_restart_api_sim_db_keeps_the_factory`: bootstrap returns the same layout and version after the restart, factory/me is not degraded, the factory is still writable.
* `test_dead_sim_degrades_gracefully_and_recovers`: with the sim stopped factory/me serves the stored snapshot (`degraded: true`), commands and bootstrap are
  503 `sim_unavailable` (circuit breaker), `/health` and the leaderboard still work; after `start sim` it recovers by itself.

`E2E_DOCKER=1 .venv/Scripts/python -m pytest test_s7_resilience.py -v`

## S4 part B (seasons, internal jobs) - `test_s4_rotation.py`

`/internal/*` must be 404 through Caddy (no header, with a token header, GET). The token-protected endpoints are called from INSIDE the stack with
`docker compose exec -T api python -c ...` (run from the repo root, using the container's own `INTERNAL_TOKEN`; the tests skip when docker is not
usable): 401 without / with a wrong token, `rotate` idempotent (`{closed: [], opened: null}` on the second call), `top/advance` (limit 1000) leaves
the version of a fresh player at 0 and the tab that last saw 0 can still send a command, `bots/grow` reports the bots. Season rotation itself
(close, results, +1 h, state alignment) is covered by `api/tests/test_seasons.py`; the e2e never moves the dev stack's season.

## S5 (leads, quiz, events) - `test_s5_leads_quiz_events.py`

Needs the API with migration 0006 (`SIM_COMMIT=$(sh scripts/simcommit.sh) docker compose up -d --build api`). DB assertions use
`docker compose exec db psql` (skipped when that fails). Leads: happy path reaches `leads` + `outbox` (`lead_created`), legacy form
(`contact` mapped by format, `ageConfirm` instead of a birth date), 401 / 404 / 403 `age_required` | `consent_required` / 422 / 409, idempotent
revoke (`lead_revoked` once), 429 on the 6th POST. Quiz: questions without answers, batch scoring, `unknown_question`, `invalid_answer_format`,
409 re-answer, season summary `quizResult` / `leadStatus`, `high_quiz` tag. Events: batch stored with props, `invalid_event_name`,
`future_timestamp`, `too_many_events`, 429 on the 11th batch.

`test_lead_quiz_events_with_sim_stopped` (needs `E2E_DOCKER=1`; runs `docker compose stop sim` and ALWAYS starts it again and waits for
`healthy`): bootstrap is 503 `sim_unavailable`, yet the lead reaches the DB (+ outbox), the quiz scores, events are stored, revoke works.

## S7 smoke suite - `test_s7_smoke.py` (+ `smoke/`)

```
cd e2e && .venv/Scripts/python -m pytest test_s7_smoke.py -q        # ~2 min (demo path ~35 s, rate-limit burst ~60 s)
E2E_DOCKER=1 .venv/Scripts/python -m pytest test_s7_smoke.py -q     # + the sim-outage scenario (stops/starts the sim container)
```

Spec S7: "smoke test of 20-50 scenarios with two tabs and a ragged network". 36 scenarios (about 170 pytest items, the bad-input
tables are parametrized). Every scenario uses fresh random dev users (`Authorization: dev <random id>`), so it needs no cleanup and
can be re-run at once. Env: `E2E_BASE`, `E2E_ORDER_WAIT_S` (as above; `0` skips the ~30 s wait for the tutorial shipment).

**Checked on every response of every scenario** (`smoke/checks.py`, an httpx response hook on each `SmokeApi` tab; a violation fails the
test at teardown with the request that caused it): `X-Request-ID` present, JSON content-type, every 4xx/5xx body is exactly
`{"error": {"code", "message", "details"}}` with a snake_case code, no stack trace / driver / framework text, never a 500 (503 only with a
documented code, and only in the degraded scenario), a 429 always carries a numeric `Retry-After`.

| Group | Scenarios |
|---|---|
| **A. Demo path** | `demo_path_end_to_end` (bootstrap -> tutorial line -> accept order -> wait for shipment -> leaderboard `me` on all 4 tabs + row in the cached top-100 -> season summary -> quiz in 2 batches -> lead -> leads/me -> summary `leadStatus` -> events -> revoke twice); `second_tab_mirrors_every_tutorial_step` (a second tab polling factory/me after each of the 22 commands is never behind and never conflicts) |
| **B. Two tabs** | `two_tabs_interleaved_commands_converge` (strict alternation on cached versions: exactly 11 x 409 -> factory/me -> retry, versions 1..12, credits exact); `tabs_racing_concurrently_lose_no_update` (2x8 and 3x5 tabs on thread barriers: one serial history, no hole in any tab's row, charged once); `passive_polling_never_conflicts` (bootstrap / advance / factory/me of tab B between A's commands); `wrong_version_is_409_and_does_not_burn_the_key` (0, ahead, 1e9, 2^63, 2^64, 1e100); `first_launch_in_many_tabs` (8 simultaneous bootstraps of a new player -> one factory, version 0); `two_players_in_parallel_do_not_interfere`; `concurrent_shift_storm_respects_the_daily_limit` (4 tabs x 3 shifts -> exactly 5 accepted, 7 `daily_limit_exceeded`) |
| **C. Ragged network** | `duplicate_requests_in_flight_apply_once` (6 identical requests at once); `lost_response_resend_after_another_tab_moved_on` (stale version + used key -> replay, not 409); `idempotency_key_scope` (same key, other command -> first result replayed; other player -> independent); `rejected_command_resent_concurrently`; `duplicate_submit_shift_gives_one_reward` (6 at once -> one `xp:10`, `shiftsLeft` 4); `duplicate_lead_and_revoke_from_three_tabs` (201/409/409, revoke 204 x3); `duplicate_quiz_submit_from_three_tabs` (200/409/409); `clients_that_hang_up_before_the_response_then_resend` (raw TLS sockets: full request, connection dropped, same key resent, 4 rounds, applied once); `truncated_and_garbage_requests_leave_the_api_healthy`; `rate_limit_bursts_429_retry_after_honoured` (advance, bootstrap, events, leads, command buckets: exactly `limit` requests pass, then 429 + `Retry-After`, other players unaffected, after waiting it works, a throttled command was not applied and its key is still usable) |
| **D. Bad input** | `command_bad_bodies_...[41 cases]` (malformed / empty / null / array / NaN / invalid UTF-8 / wrong content-type; version str, float, bool, null, missing, negative; key int, null, 7, 65 and 100 000 chars; cmd str, null, `{}`, unknown or wrongly cased type, wrong field types, bad enums, 1.9 MB padding) -> clean 422 and the factory is unchanged; `command_accepts_odd_but_valid_input` (emoji, markup, SQL text, non-ASCII key); `giant_factory_name_is_truncated_not_stored`; `oversize_bodies` (API 413 on /api/events, Caddy 413 above 2 MB); `odd_methods_paths_and_queries` (405 / 404 / 422 forms, NULs, 5000-char values, overflowing ids); `advance_bad_bodies`; `other_endpoints_bad_bodies[30 cases]` (quiz / events / leads); `dev_auth_header_edge_cases`; `request_id_is_echoed_or_replaced`; `fuzz_every_command_type_with_poison_never_5xx` (all 28 Command types x 4 variants, 112 commands, none slow, none 5xx) |
| **E. Auth** | `no_or_bad_auth_is_401_on_every_route[14 routes]` (no header, garbage scheme, malformed dev id, bad initData; 503 `config_error` is accepted for `tma` when the stack has no BOT_TOKEN); `every_route_with_valid_auth_is_alive`; `player_data_is_isolated_between_users` |
| **F. Every response** | `gzip_when_the_client_accepts_it_and_the_body_is_large` (gzip + `Vary` on factory/me, bootstrap, leaderboard; identity untouched; tiny bodies not compressed); `health_and_ready_are_public_and_clean` (and `/internal/*` is 404 from outside) |
| **G. Degraded** (`E2E_DOCKER=1`) | `sim_outage_mixed_traffic_and_recovery`: sim stopped -> factory/me `degraded`, leaderboard / summary / leads / quiz / events / health fine, game writes and a new player's bootstrap are clean 503 `sim_unavailable` (breaker `circuit_open` + `Retry-After` appear), no half-made factory; `start sim` -> recovers by itself, nothing lost or doubled. Restart-the-whole-stack and the basic dead-sim checks are in `test_s7_resilience.py` / `test_s5_leads_quiz_events.py`, not repeated here |

**Known product bugs** found by the suite are kept as `xfail(strict=False)` items with the exact request in the reason (they turn into
XPASS when fixed; run `pytest test_s7_smoke.py -q -rx` to list them): NUL characters in `idempotency_key` / `renameFactory.name` / the
`university` query -> 500; `GET /api/factory/<5000 digits>` -> 500; `Authorization: dev <tg_id above 2^63-1>` -> 500.
