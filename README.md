# Mosprom: backend for the Mini App game

Server-authoritative backend for the Telegram Mini App (factory tycoon). The client only sends commands, the server owns the
state. Product spec: `docs/spec-rev4.txt`; interfaces between the parts: `docs/ARCHITECTURE.md`; progress: `PROGRESS.md`.

| Part | What | Stack |
|---|---|---|
| `api/` | public HTTP API: auth (Telegram initData / dev), commands, bootstrap, leads, quiz, events | Python 3.12, FastAPI, SQLAlchemy 2 async, asyncpg, Alembic, Pydantic v2 |
| `sim/` | stateless game simulation over internal HTTP; imports `vendor/frontend/web/src/lib` unchanged | Node 22, TypeScript, cluster workers |
| `bot/` | Telegram bot, the single owner of all background jobs (APScheduler) | aiogram 3, APScheduler 3 |
| `contract/` | JSON Schema + Pydantic generated from the frontend TypeScript contract | ts-json-schema-generator, datamodel-code-generator |
| `vendor/frontend/` | read-only git submodule with the game client (`git submodule update --init`) | |

## For the jury

**What it is.** The backend of a Telegram Mini App game: the player builds a kvass bottling factory, the server owns the whole
game state and the client only sends commands. Around the game: a season leaderboard (with demo bots), an 18+ application form
(lead) with consent, a quiz and analytics events, and an HR bot that receives the leads. Spec: `docs/spec-rev4.txt`.

```
 Mini App (browser) --HTTPS--> caddy --> api (FastAPI, 2 workers) --> db     Postgres 16: users, factories (state JSONB),
                               TLS        |   |   |                          seasons, leads, quiz, events, outbox
   only /api/*, /health, /ready           |   |   +-------------------> redis  rate limit, idempotency fast-path, top-100 cache
   are proxied; /internal/* never         |   +-----------------------> sim    Node, stateless game engine (the client's own
                                          |        3 s timeout + circuit breaker                          lib/sim, unchanged)
 Telegram <--> bot (aiogram + APScheduler) --X-Internal-Token--> api /internal/*  season rotation, top-200 catch-up, bots growth;
                HR cards, /leads /export /funnel                                   reads the DB directly for analytics
```

**Run (about 2 minutes; needs Docker and `git submodule update --init`).**

```sh
cp .env.example .env
SIM_COMMIT=$(sh scripts/simcommit.sh) docker compose up -d --build
curl -k https://localhost/ready                      # {"status":"ready","checks":{"db":"ok","migrations":"ok","redis":"ok","sim":"ok","config":"ok"}}
docker compose exec api python -m app.seed_bots      # 40 demo bots, so the leaderboard is not empty
```

**Demo (dev auth, no Telegram needed; `DEV_AUTH=1` is the default of `.env.example`).** Header `Authorization: dev <tg_id>:<name>`.

```sh
H='Authorization: dev 1001:Ivan'
curl -sk -H "$H" https://localhost/api/bootstrap                                  # creates the factory, returns version + snapshot
curl -sk -H "$H" 'https://localhost/api/season/leaderboard?tab=general&limit=5'   # tabs: general | quality | speed | uni
curl -sk -H "$H" https://localhost/api/season/summary
cd e2e && py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pytest
                                                                                  # plays the whole tutorial against the server
```

Show the resilience (what "stabilisation" means here): `docker compose restart api sim db`, then `/api/bootstrap` again - the
factory and its version are unchanged. `docker compose stop sim` - `/api/factory/me` keeps answering from the last stored snapshot
(`"degraded": true`), leads, quiz, events and the leaderboard do not notice, game commands answer `503 sim_unavailable`
instantly (circuit breaker). `docker compose start sim` - it heals by itself within ~10 s.

**Endpoints** (every error is `{"error": {"code", "message", "details"}}` + `X-Request-ID`; a 429 always has `Retry-After`).

| Endpoint | What |
|---|---|
| `GET /health`, `GET /ready` | liveness; readiness of Postgres, migrations, Redis, sim and required env |
| `GET /api/bootstrap` | creates user + factory, 8 h offline catch-up, season, feature flags, snapshot |
| `POST /api/command` | game command with `version` (409 `version_conflict`) and `idempotency_key` (replay-safe); the server validates `submitShift` |
| `POST /api/advance`, `GET /api/factory/me` | lazy catch-up; `factory/me` falls back to the stored snapshot while the sim is down |
| `GET /api/season/leaderboard`, `/api/season/summary`, `/api/factory/{id}` | ranking (4 tabs, gap to place 51), season result, public view of a factory |
| `POST /api/leads`, `GET`/`DELETE /api/leads/me` | application form (18+, consent, one per season), consent withdrawal (152-FZ); independent of the sim |
| `GET /api/quiz/questions`, `POST /api/quiz/answer` | quiz with server-side answers |
| `POST /api/events` | analytics batches (name whitelist, up to 50 events) |
| `POST /internal/seasons/rotate`, `/internal/top/advance`, `/internal/bots/grow` | bot only (token); not reachable through caddy |

**Reliability (S7).** DB statement timeout 3 s and lock timeout 2 s (`503 db_unavailable`), sim timeout 3 s, whole request 5 s
(`503 timeout` + `Retry-After: 1`; `/internal/*` exempt), requests over 1 s are logged at WARNING with `duration_ms`, sim circuit
breaker (5 consecutive failures, fail fast for 10 s, then one probe; state in the logs as `breaker_state`), JSON logs with
`request_id`, `restart: unless-stopped` everywhere, bounded workers (`API_WORKERS`, `SIM_WORKERS` <= 4), optional DB backups
(see Operations). The knobs are listed in `.env.example`.

**Where the tests are.** `api/tests` (pytest: every endpoint, error codes, idempotency, concurrency, breaker, timeouts; needs the
throwaway Postgres/Redis of `api/tests/conftest.py`), `sim/` (`npm run check`), `contract/` (generated schema vs. the client's
TypeScript), `bot/tests`, `e2e/` (against the running stack: tutorial, two tabs, 409 resync, leaderboard; `E2E_DOCKER=1` adds the
restart / dead-sim scenarios of `e2e/test_s7_resilience.py`). Commands: see "Tests" below.

**Known limitations.**
* Waiting for the lead's sim work (S1 / S1U): **undo / redo are hidden** (`features.undo=false`, the server answers
  `history_unavailable` until the history lives in the factory state) and **server-computed order ETAs are not in the snapshot yet**
  (`features.serverOrderEta=false`; the client keeps estimating them itself).
* `speed` in the leaderboard is derived from produced bottles over time (the snapshot carries no flow rate yet).
* Demo bots are fillers (`BOTS_ENABLED=0` hides them everywhere) and are excluded from every analytics number.
* This stack does not host the Mini App web build; Telegram needs a real HTTPS domain (`DOMAIN`), a tunnel is the last resort.
* One Postgres on one host: backups protect against data loss, not against losing the machine (copy `./backups` elsewhere).

## Run

Requirements: Docker with Compose v2, Git (submodule initialised).

```sh
cp .env.example .env                                  # dev defaults: APP_ENV=dev, DEV_AUTH=1, DOMAIN=localhost
SIM_COMMIT=$(scripts/simcommit.sh) docker compose up -d --build
docker compose ps                                     # everything healthy; `migrate` exits 0
curl -k https://localhost/health                      # caddy -> api  (-k: caddy's local CA for DOMAIN=localhost)
curl -k https://localhost/ready                       # db, migrations, redis, sim, config checks
```

PowerShell: `$env:SIM_COMMIT = (git -C vendor/frontend rev-parse --short HEAD); docker compose up -d --build`.
`SIM_COMMIT` pins the sim image (`mosprom-sim:<sha>`) to the vendored frontend commit (lib/sim), so a submodule bump is an explicit rebuild.
Without it the image is tagged `mosprom-sim:dev`.

Local auth: header `Authorization: dev 123:Ivan` (only while `DEV_AUTH=1` and `APP_ENV=dev`).

| Service | Role | Host access |
|---|---|---|
| `caddy` | TLS (automatic HTTPS), gzip/zstd, proxies `/api/*`, `/health`, `/ready` to `api:8000` | `80`, `443` (`HTTP_PORT`/`HTTPS_PORT` in `.env`) |
| `api` | FastAPI, `API_WORKERS` (2) uvicorn workers | internal `:8000` |
| `migrate` | one-shot `alembic upgrade head`, `api` waits for it | - |
| `db` | `postgres:16-alpine`, volume `pgdata` | `127.0.0.1:55432` (`mosprom`/`mosprom`) |
| `redis` | `redis:7-alpine`, caches and rate-limit counters only, no persistence | `127.0.0.1:56379` |
| `sim` | game simulation, `/health` reports `simCommit` | internal `:8080`, `127.0.0.1:58080` for debugging |
| `bot` | aiogram + scheduler; idles (no crash loop) while `BOT_TOKEN` is empty | - |

Optional profiles:

* `pgbouncer` (transaction pooling): set `DB_HOST=pgbouncer`, `DB_PORT=6432`, `PGBOUNCER=1` in `.env`, then
  `docker compose --profile pgbouncer up -d --build`. `migrate` always talks to `db` directly.
* `sim-replicas`: `docker compose --profile sim-replicas up -d` adds a second sim container behind the same DNS name `sim`.
* `backup`: periodic `pg_dump` into `./backups` (see Operations).

Production: set a real `DOMAIN` (DNS A record to the host, ports 80/443 open), `APP_ENV=prod`, `DEV_AUTH=0`, a real `BOT_TOKEN`,
`WEBAPP_URL`, strong `POSTGRES_PASSWORD`. Telegram opens a Mini App only over HTTPS on a real domain. Port 80/443 busy on Windows?
Change `HTTP_PORT`/`HTTPS_PORT` in `.env`. Reset everything including data: `docker compose down -v`.

### Operations

* **Backups.** `docker compose --profile backup up -d backup` dumps the database (`pg_dump -Fc`) into `./backups` right away and
  then every `BACKUP_INTERVAL_H` (6) hours, keeping the newest `BACKUP_KEEP` (28). By hand: `sh scripts/backup.sh`.
  Restore (stops api + bot, safety dump first, runs the migrations afterwards): `sh scripts/restore.sh backups/<file>.dump`.
* **Internal token.** `INTERNAL_TOKEN` is shared by api and bot (`/internal/*`, header `X-Internal-Token`). The dev placeholder
  works only with `APP_ENV=dev`; the api refuses to start with `APP_ENV=prod` unless it is a real secret (`openssl rand -hex 24`).
* **Workers.** `API_WORKERS` (default 2, max 4: every worker keeps a DB pool of 10), `SIM_WORKERS` (default 2, max 4).
* **Logs.** `docker compose logs -f api`: one JSON line per request (`request_id`, `user_id`, `status`, `error_code`,
  `duration_ms`); breaker transitions carry `breaker_state`; `"msg": "slow request"` marks anything over 1 s.

## Contract (client commands and snapshot)

Pydantic models are generated from `vendor/frontend/web/src/lib/api/types.ts`; never edit `api/app/contract/generated/`.

```sh
contract/generate.sh     # regenerate schema + models (run after bumping the vendor/frontend submodule)
contract/check.sh        # CI / pre-commit: exit 1 if the committed contract differs from a fresh generation
```

Details: `contract/README.md`.

## Tests

| Part | Command |
|---|---|
| contract | `cd contract && py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt && .venv/Scripts/python -m pytest` (add `SIM_URL=http://127.0.0.1:58080` with the stack up for the live sim snapshot check) |
| bot | `cd bot && py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt && .venv/Scripts/python -m pytest` |
| api | `cd api && py -3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements-dev.txt && .venv/Scripts/python -m pytest`; Postgres/Redis tests need the throwaway containers from the docstring of `api/tests/conftest.py` (ports 55433 / 56380), otherwise they skip |
| sim | `cd sim && npm ci && npm run check` (typecheck, tests, bundle) |

On Linux/macOS use `.venv/bin/python` instead of `.venv/Scripts/python`.

## Layout

```
api/  sim/  bot/  contract/  scripts/  docs/  vendor/frontend/   (see docs/ARCHITECTURE.md)
docker-compose.yml  Caddyfile  .env.example  .dockerignore  PROGRESS.md
```

`scripts/simcommit.sh` prints the vendored frontend commit (`git -C vendor/frontend rev-parse --short HEAD`).
