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
| `api` | FastAPI, 2 uvicorn workers | internal `:8000` |
| `migrate` | one-shot `alembic upgrade head`, `api` waits for it | - |
| `db` | `postgres:16-alpine`, volume `pgdata` | `127.0.0.1:55432` (`mosprom`/`mosprom`) |
| `redis` | `redis:7-alpine`, caches and rate-limit counters only, no persistence | `127.0.0.1:56379` |
| `sim` | game simulation, `/health` reports `simCommit` | internal `:8080`, `127.0.0.1:58080` for debugging |
| `bot` | aiogram + scheduler; idles (no crash loop) while `BOT_TOKEN` is empty | - |

Optional profiles:

* `pgbouncer` (transaction pooling): set `DB_HOST=pgbouncer`, `DB_PORT=6432`, `PGBOUNCER=1` in `.env`, then
  `docker compose --profile pgbouncer up -d --build`. `migrate` always talks to `db` directly.
* `sim-replicas`: `docker compose --profile sim-replicas up -d` adds a second sim container behind the same DNS name `sim`.

Production: set a real `DOMAIN` (DNS A record to the host, ports 80/443 open), `APP_ENV=prod`, `DEV_AUTH=0`, a real `BOT_TOKEN`,
`WEBAPP_URL`, strong `POSTGRES_PASSWORD`. Telegram opens a Mini App only over HTTPS on a real domain. Port 80/443 busy on Windows?
Change `HTTP_PORT`/`HTTPS_PORT` in `.env`. Reset everything including data: `docker compose down -v`.

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
