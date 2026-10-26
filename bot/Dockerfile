FROM docker.io/library/python:3.15-alpine3.24@sha256:d44b1254cb92329418d164b22b20d86bbd3869d48e80e09e476531a409a728bb

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# build context is ./bot
COPY requirements.lock .
RUN pip install --require-hashes -r requirements.lock

COPY app ./app

RUN addgroup -g 10002 bot && adduser -D -H -u 10002 -G bot bot
USER bot

# Without BOT_TOKEN the process idles (does not exit), so compose stays green locally.
CMD ["python", "-m", "app.main"]
