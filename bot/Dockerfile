FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /srv

# build context is ./bot
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 10002 --no-create-home bot
USER bot

# Without BOT_TOKEN the process idles (does not exit), so compose stays green locally.
CMD ["python", "-m", "app.main"]
