FROM docker.io/library/python:3.12-slim@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# build context is ./bot
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock

COPY app ./app

RUN useradd --system --uid 10002 --no-create-home bot
USER bot

# Without BOT_TOKEN the process idles (does not exit), so compose stays green locally.
CMD ["python", "-m", "app.main"]
