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
