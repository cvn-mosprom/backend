#!/usr/bin/env sh
# Prints the short commit of the vendored frontend (vendor/frontend, i.e. the lib/sim the sim image runs).
# Pin the sim image to it:
#   SIM_COMMIT=$(scripts/simcommit.sh) docker compose up -d --build
set -eu
cd "$(dirname "$0")/.."
git -C vendor/frontend rev-parse --short HEAD
