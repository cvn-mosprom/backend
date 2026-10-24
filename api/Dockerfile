FROM docker.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

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

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# The API is only reachable through caddy / the compose network, so trust X-Forwarded-* from any peer.
# Worker processes come from WEB_CONCURRENCY (uvicorn reads it; compose sets it from API_WORKERS; match CPU/RAM and the database connection budget).
# Migrations: same image, `command: alembic upgrade head` (WORKDIR /app holds alembic.ini; needs DATABASE_URL).
CMD ["/app/entrypoint.sh"]
