#!/bin/sh
# SeaweedFS reads its identities only from a file, and that file cannot read the environment. So s3.json in this directory
# is a TEMPLATE: this wrapper substitutes MEDIA_S3_ACCESS_KEY / MEDIA_S3_SECRET_KEY into /tmp/s3.json (the bind mount stays
# read-only and the secret never lands in git) and hands over to the image's own entrypoint. Keys are restricted to
# [A-Za-z0-9_-] (>= 8 chars) so the substitution below cannot break the JSON.
set -eu

: "${MEDIA_S3_ACCESS_KEY:?MEDIA_S3_ACCESS_KEY is not set}"
: "${MEDIA_S3_SECRET_KEY:?MEDIA_S3_SECRET_KEY is not set}"
for v in "$MEDIA_S3_ACCESS_KEY" "$MEDIA_S3_SECRET_KEY"; do
  case "$v" in *[!A-Za-z0-9_-]*) echo "seaweedfs: MEDIA_S3_* keys must be [A-Za-z0-9_-] only" >&2; exit 1 ;; esac
  [ "${#v}" -ge 8 ] || { echo "seaweedfs: MEDIA_S3_* keys must be >= 8 chars" >&2; exit 1; }
done

sed -e "s|\${MEDIA_S3_ACCESS_KEY}|$MEDIA_S3_ACCESS_KEY|g" -e "s|\${MEDIA_S3_SECRET_KEY}|$MEDIA_S3_SECRET_KEY|g" \
  /etc/seaweedfs/s3.json > /tmp/s3.json
chmod 600 /tmp/s3.json
exec /entrypoint.sh "$@"
