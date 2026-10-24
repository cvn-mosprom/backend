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
