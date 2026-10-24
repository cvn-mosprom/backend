FROM docker.io/library/python:3.12-alpine3.24@sha256:b658b05ce9c4eb97ec6fc19424818a1f31993659c2b505a532f470e83807815d

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# constraints.txt pins the versions the test-suite ran against (pip freeze of the dev venv)
COPY requirements.lock ./
RUN pip install --require-hashes -r requirements.lock

# build context is ./api
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app
COPY --chmod=755 entrypoint.sh ./entrypoint.sh

RUN addgroup -g 10001 app && adduser -D -H -u 10001 -G app app
USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# The API is only reachable through caddy / the compose network, so trust X-Forwarded-* from any peer.
# Worker processes come from WEB_CONCURRENCY (uvicorn reads it; compose sets it from API_WORKERS; match CPU/RAM and the database connection budget).
# Migrations: same image, `command: alembic upgrade head` (WORKDIR /app holds alembic.ini; needs DATABASE_URL).
CMD ["/app/entrypoint.sh"]
