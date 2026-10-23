#!/usr/bin/env sh
# Drift check: regenerate into a temp dir and diff against the committed contract.
# Exit 0 = in sync, exit 1 = drift (run contract/generate.sh and commit the result).
set -eu

cd "$(dirname "$0")"
HERE=$(pwd)
GEN_DIR="$HERE/../api/app/contract/generated"

TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
# Git Bash on Windows: hand node a Windows-style path.
if command -v cygpath >/dev/null 2>&1; then TMP_NATIVE=$(cygpath -m "$TMP"); else TMP_NATIVE=$TMP; fi

sh ./generate.sh --schema-dir "$TMP_NATIVE/schema" --py-dir "$TMP_NATIVE/generated" >/dev/null

rc=0
diff -ru "$HERE/schema" "$TMP/schema" || rc=1
diff -ru -x __pycache__ "$GEN_DIR" "$TMP/generated" || rc=1

if [ "$rc" -ne 0 ]; then
  echo "contract drift detected: run contract/generate.sh and commit the result" >&2
  exit 1
fi
echo "contract in sync"
