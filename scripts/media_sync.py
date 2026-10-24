#!/usr/bin/env python3
"""Upload the site media (landing/labels/audio/icons) from the frontend's static dir into SeaweedFS (S3, path-style).

Runs as the one-shot compose service `media-sync` (python:3.12-slim + boto3, see deploy/media-sync/Dockerfile), or by hand:

    MEDIA_S3_ENDPOINT=http://127.0.0.1:8333 MEDIA_S3_ACCESS_KEY=... MEDIA_S3_SECRET_KEY=... \
        python scripts/media_sync.py --source vendor/frontend/web/static --prune

* creates the bucket if missing; object key = the path relative to --source (landing/hero.mp4 -> media/landing/hero.mp4), which
  is exactly the URL path the frontend uses, so Caddy only has to prefix `/media`;
* Content-Type by extension, Cache-Control `public, max-age=86400` (files are not content-hashed, so not `immutable`);
* idempotent: an object whose ETag equals the file's md5 (and whose Content-Type / Cache-Control are current) is skipped;
* --prune (env MEDIA_SYNC_PRUNE=1) deletes objects that are no longer in the source (refuses to run on an empty source);
* prints one summary line; exit 1 if anything failed.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time
from pathlib import Path

DIRS = ("landing", "labels", "audio", "icons")
CACHE_CONTROL = "public, max-age=86400"
CONTENT_TYPES = {
    ".mp4": "video/mp4", ".webp": "image/webp", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".json": "application/json", ".bin": "application/octet-stream", ".js": "text/javascript", ".txt": "text/plain",
}


def content_type(name: str) -> str:
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), "application/octet-stream")


def md5_of(path: Path) -> str:
    h = hashlib.md5()  # noqa: S324 - the S3 ETag, not a security hash
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def local_files(source: Path, dirs=DIRS) -> dict[str, Path]:
    """key (posix path relative to source) -> file, for every file under the given top-level dirs."""
    out: dict[str, Path] = {}
    for d in dirs:
        for p in sorted((source / d).rglob("*")):
            if p.is_file():
                out[p.relative_to(source).as_posix()] = p
    return out


def retry(fn, *, tries: int = 6, delay: float = 1.5):
    """SeaweedFS needs a few seconds after boot before it can allocate its first volume ("no writable volumes")."""
    for i in range(tries):
        try:
            return fn()
        except Exception:  # botocore ClientError / EndpointConnectionError: all worth another try
            if i == tries - 1:
                raise
            time.sleep(delay * (i + 1))


def remote_objects(client, bucket: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    token = None
    while True:
        kw = {"Bucket": bucket, **({"ContinuationToken": token} if token else {})}
        page = retry(lambda: client.list_objects_v2(**kw))
        for o in page.get("Contents", []):
            out[o["Key"]] = {"etag": o["ETag"].strip('"'), "size": o["Size"]}
        if not page.get("IsTruncated"):
            return out
        token = page["NextContinuationToken"]


def ensure_bucket(client, bucket: str) -> bool:
    """True when it had to be created."""
    try:
        retry(lambda: client.head_bucket(Bucket=bucket))
        return False
    except Exception as e:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        if code not in ("404", "NoSuchBucket", "NotFound"):
            raise
    retry(lambda: client.create_bucket(Bucket=bucket))
    return True


def is_current(client, bucket: str, key: str, remote: dict, md5: str, size: int, ctype: str) -> bool:
    """Same bytes (ETag = md5) and the same headers we would upload now (so a changed policy re-uploads)."""
    if remote["etag"] != md5 or remote["size"] != size:
        return False
    head = retry(lambda: client.head_object(Bucket=bucket, Key=key))
    return head.get("ContentType") == ctype and head.get("CacheControl") == CACHE_CONTROL


def sync(client, bucket: str, source: Path, *, prune: bool = False, dirs=DIRS, log=print) -> dict:
    files = local_files(source, dirs)
    if not files:
        raise SystemExit(f"media-sync: no files under {source} ({', '.join(dirs)}); is the vendor/frontend submodule checked out?")
    stats = {"files": len(files), "uploaded": 0, "uploaded_bytes": 0, "skipped": 0, "deleted": 0, "failed": 0}
    stats["bucket_created"] = ensure_bucket(client, bucket)
    remote = remote_objects(client, bucket)
    for key, path in files.items():
        try:
            size, md5, ctype = path.stat().st_size, md5_of(path), content_type(path.name)
            if key in remote and is_current(client, bucket, key, remote[key], md5, size, ctype):
                stats["skipped"] += 1
                continue
            body = path.read_bytes()
            retry(lambda: client.put_object(Bucket=bucket, Key=key, Body=body, ContentType=ctype, CacheControl=CACHE_CONTROL))
            stats["uploaded"] += 1
            stats["uploaded_bytes"] += size
            log(f"  put {key} ({size} B, {ctype})")
        except Exception as e:
            stats["failed"] += 1
            log(f"  FAILED {key}: {type(e).__name__}: {e}")
    if prune:
        for key in sorted(set(remote) - set(files)):
            try:
                retry(lambda: client.delete_object(Bucket=bucket, Key=key))
                stats["deleted"] += 1
                log(f"  del {key}")
            except Exception as e:
                stats["failed"] += 1
                log(f"  FAILED delete {key}: {type(e).__name__}: {e}")
    return stats


def make_client(endpoint: str, access_key: str, secret_key: str):
    import boto3  # imported lazily: the unit tests drive sync() with a fake client and need no boto3
    from botocore.client import Config

    return boto3.client(
        "s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key, region_name="us-east-1",
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}, retries={"max_attempts": 2},
                      connect_timeout=5, read_timeout=60),
    )


def main(argv=None) -> int:
    env = os.environ.get
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--endpoint", default=env("MEDIA_S3_ENDPOINT", "http://seaweedfs:8333"))
    ap.add_argument("--bucket", default=env("MEDIA_BUCKET", "media"))
    ap.add_argument("--source", default=env("MEDIA_SOURCE_DIR", "/src"))
    ap.add_argument("--prune", action="store_true", default=env("MEDIA_SYNC_PRUNE", "0") == "1")
    args = ap.parse_args(argv)
    access, secret = env("MEDIA_S3_ACCESS_KEY"), env("MEDIA_S3_SECRET_KEY")
    if not access or not secret:
        print("media-sync: MEDIA_S3_ACCESS_KEY / MEDIA_S3_SECRET_KEY are not set", file=sys.stderr)
        return 2
    t0 = time.monotonic()
    s = sync(make_client(args.endpoint, access, secret), args.bucket, Path(args.source), prune=args.prune)
    print(f"media-sync: bucket={args.bucket} files={s['files']} uploaded={s['uploaded']} ({s['uploaded_bytes']} B) "
          f"unchanged={s['skipped']} deleted={s['deleted']} failed={s['failed']} bucket_created={int(s['bucket_created'])} "
          f"in {time.monotonic() - t0:.1f}s")
    return 1 if s["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
