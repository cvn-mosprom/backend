# Progress

Resume rule: read this file, docs/ARCHITECTURE.md and docs/spec-rev4.txt, then continue from "Next".

## Done
- Repo scaffold, frontend pinned as submodule vendor/frontend (dde6cbf).
- S0 done: compose (caddy api db migrate redis sim bot + pgbouncer/sim-replicas profiles), health/ready, initData + dev auth, errors, request id, JSON logs, Redis rate limiter, generated contract (28 cmds), sim service. Tests: api 125, sim 55, contract 212, bot 19.

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
- S2 backend core: bootstrap, command, advance, factory/me, idempotency, shifts_left in state, HttpGameApi notes.
