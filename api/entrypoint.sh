#!/bin/sh
set -eu
if [ "${APP_ENV:-dev}" = prod ]; then python -m app.preflight; fi
mkdir -p /tmp/prometheus
find /tmp/prometheus -name '*.db' -delete
exec uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips '*' --no-access-log
