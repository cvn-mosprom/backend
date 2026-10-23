#!/usr/bin/env sh
# Regenerate the contract: TS types -> JSON Schema -> Pydantic models.
#
#   contract/generate.sh                      # writes contract/schema/*.json + api/app/contract/generated/*
#   contract/generate.sh --schema-only        # JSON Schema only (no python needed)
#   contract/generate.sh --schema-dir D1 --py-dir D2   # custom output dirs (used by check.sh)
#
# Bootstraps contract/node_modules (npm ci) and contract/.venv (py -3.12 / python3.12) on first run.
set -eu

cd "$(dirname "$0")"

# ---- node deps
if [ ! -d node_modules/ts-json-schema-generator ]; then
  echo "[contract] installing node deps (npm ci)"
  if [ -f package-lock.json ]; then npm ci --no-audit --no-fund; else npm install --no-audit --no-fund; fi
fi

# ---- python venv (skipped for --schema-only)
case " $* " in
  *" --schema-only "*) ;;
  *)
    if [ ! -x .venv/Scripts/python.exe ] && [ ! -x .venv/bin/python ]; then
      echo "[contract] creating .venv"
      if command -v py >/dev/null 2>&1; then py -3.12 -m venv .venv
      elif command -v python3.12 >/dev/null 2>&1; then python3.12 -m venv .venv
      else python3 -m venv .venv; fi
      if [ -x .venv/Scripts/python.exe ]; then PY=.venv/Scripts/python.exe; else PY=.venv/bin/python; fi
      "$PY" -m pip install -q --upgrade pip
      "$PY" -m pip install -q -r requirements.txt
    fi
    ;;
esac

exec node generate.mjs "$@"
