FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    WEB_CONCURRENCY=2 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# constraints.txt pins the versions the test-suite ran against (pip freeze of the dev venv)
COPY requirements.txt constraints.txt ./
RUN pip install -r requirements.txt -c constraints.txt

# build context is ./api
COPY alembic.ini ./
COPY alembic ./alembic
COPY app ./app

RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000

HEALTHCHECK --interval=15s --timeout=4s --start-period=15s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"

# The API is only reachable through caddy / the compose network, so trust X-Forwarded-* from any peer.
# Worker processes come from WEB_CONCURRENCY (uvicorn reads it; compose sets it from API_WORKERS, keep <= 4).
# Migrations: same image, `command: alembic upgrade head` (WORKDIR /app holds alembic.ini; needs DATABASE_URL).
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log"]
