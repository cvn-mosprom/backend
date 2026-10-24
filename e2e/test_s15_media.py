"""S15 against the running stack: the site media (landing/labels/audio/icons) come from SeaweedFS through Caddy, byte-identical to
vendor/frontend/web/static; the web image no longer carries them; media-sync is idempotent and prunes. The script logic
(scripts/media_sync.py) also has fast unit tests against a fake S3 at the bottom (no stack needed).

Needs `docker compose` for the image / media-sync checks (those skip when it is unavailable); everything else is plain HTTP on E2E_BASE.
"""

from __future__ import annotations

import hashlib
import importlib.util
import os
import re
import subprocess
from pathlib import Path

import httpx
import pytest
from conftest import BASE

ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "vendor" / "frontend" / "web" / "static"
DIRS = ("landing", "labels", "audio", "icons")
FILES = {p.relative_to(STATIC).as_posix(): p for d in DIRS for p in sorted((STATIC / d).rglob("*")) if p.is_file()}
# spelled out here on purpose (not imported from the script): a typo in the script's table must fail the test
TYPES = {".mp4": "video/mp4", ".webp": "image/webp", ".jpg": "image/jpeg", ".png": "image/png", ".json": "application/json",
         ".bin": "application/octet-stream", ".js": "text/javascript", ".txt": "text/plain"}
VIDEOS = [k for k in FILES if k.endswith(".mp4")]


def test_the_source_has_the_expected_media():
    assert len(FILES) >= 80 and len(VIDEOS) == 5, "vendor/frontend submodule not checked out?"
    assert {Path(k).suffix for k in FILES} <= set(TYPES), "a new file type needs an entry in media_sync.CONTENT_TYPES"


@pytest.fixture(scope="module")
def web():
    with httpx.Client(base_url=BASE, verify=False, timeout=60.0, headers={"Accept-Encoding": "identity"}) as c:
        yield c


def compose(*args: str, timeout: int = 240) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["docker", "compose", *args], cwd=ROOT, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as e:
        pytest.skip(f"docker compose unavailable: {e}")


# ---------------------------------------------------------------- served by SeaweedFS through Caddy

@pytest.mark.parametrize("key", FILES)
def test_every_media_file_is_served_byte_identical(web, key):
    local = FILES[key]
    r = web.get("/" + key)
    assert r.status_code == 200, r.text[:200]
    assert r.headers["content-type"].split(";")[0] == TYPES[local.suffix]
    assert hashlib.sha256(r.content).hexdigest() == hashlib.sha256(local.read_bytes()).hexdigest()
    assert "SeaweedFS" in r.headers["server"], "not answered by SeaweedFS"
    cc = r.headers["cache-control"]
    assert "max-age=86400" in cc and "immutable" not in cc  # files are not content-hashed
    assert r.headers.get("accept-ranges") == "bytes"


@pytest.mark.parametrize("key", VIDEOS)
def test_video_range_requests_give_206(web, key):
    data = FILES[key].read_bytes()
    r = web.get("/" + key, headers={"Range": "bytes=0-99"})
    assert r.status_code == 206 and r.content == data[:100]
    assert r.headers["content-range"] == f"bytes 0-99/{len(data)}" and r.headers["content-type"] == "video/mp4"
    tail = web.get("/" + key, headers={"Range": "bytes=-50"})  # what a player does to find the moov atom
    assert tail.status_code == 206 and tail.content == data[-50:]
    mid = web.get("/" + key, headers={"Range": f"bytes={len(data) // 2}-"})
    assert mid.status_code == 206 and mid.content == data[len(data) // 2:]


def test_head_and_unsatisfiable_range(web):
    size = FILES["landing/hero.mp4"].stat().st_size
    h = web.head("/landing/hero.mp4")
    assert h.status_code == 200 and int(h.headers["content-length"]) == size and h.content == b""
    assert web.get("/landing/hero.mp4", headers={"Range": f"bytes={size + 10}-"}).status_code == 416


def test_cache_buster_query_is_fine_other_queries_are_not(web):
    assert web.get("/icons/buildings.png?v=12").status_code == 200
    for q in ("?tagging", "?uploads", "?list-type=2", "?v=1&acl", "?acl="):
        assert web.get("/audio/samples.json" + q).status_code == 404, q


def test_missing_media_is_a_plain_404_not_s3_xml_nor_the_spa(web):
    for path in ("/landing/nope.png", "/labels/pixel/missing_6.png", "/audio/nope.bin", "/icons/nope.json",
                 "/labels/", "/audio/", "/icons/", "/labels/pixel/"):
        r = web.get(path)
        assert r.status_code == 404 and r.text.strip() == "not found", (path, r.status_code, r.text[:80])
        assert "xml" not in r.headers["content-type"] and "SeaweedFS" not in r.headers.get("server", "")


def test_media_is_read_only_through_caddy(web):
    for method in ("PUT", "POST", "DELETE", "PATCH"):
        assert web.request(method, "/landing/hero.mp4", content=b"x").status_code == 405, method
    assert web.get("/landing/hero.mp4", headers={"Range": "bytes=0-9"}).status_code == 206  # still intact


def test_the_spa_and_other_routes_are_unaffected(web):
    for path in ("/", "/app", "/play", "/privacy", "/fonts/ToshibaSat_8x14.woff"):
        assert web.get(path).status_code == 200, path
    r = web.get("/")
    assert r.headers["content-type"].startswith("text/html")  # prerendered page, not the media prefix
    assert web.get("/internal").status_code == 404 and web.get("/internal/x").status_code == 404
    assert web.get("/health").json()["status"] == "ok"


def test_ready_reports_media_without_depending_on_it(web):
    r = web.get("/ready")
    assert r.status_code == 200 and r.json()["checks"]["media"] == "ok"


@pytest.mark.skipif(os.environ.get("E2E_DOCKER") != "1", reason="set E2E_DOCKER=1 to stop/start the seaweedfs container")
def test_media_outage_does_not_take_the_game_down(web):
    """SeaweedFS stopped: media paths fail (502/504, not the SPA shell), /ready only reports it, the game and the app keep working."""
    assert compose("stop", "seaweedfs").returncode == 0
    try:
        r = web.get("/landing/hero.mp4")
        assert r.status_code in (502, 503, 504)
        ready = web.get("/ready")
        assert ready.status_code == 200 and ready.json()["checks"]["media"].startswith("error")
        assert web.get("/play").status_code == 200 and web.get("/health").json()["status"] == "ok"
        assert web.get("/api/bootstrap", headers={"Authorization": "dev 515151:e2e"}).status_code == 200
    finally:
        assert compose("start", "seaweedfs").returncode == 0
    for _ in range(40):  # SeaweedFS needs ~25 s to elect its raft leader again
        if web.get("/landing/hero.mp4", headers={"Range": "bytes=0-9"}).status_code == 206:
            break
        import time

        time.sleep(3)
    else:
        pytest.fail("media did not come back after seaweedfs was started")


# ---------------------------------------------------------------- the web image no longer carries the media

def test_caddy_image_does_not_contain_the_media():
    for d in DIRS:
        r = compose("exec", "-T", "caddy", "ls", f"/srv/{d}")
        assert r.returncode != 0, f"/srv/{d} is still in the caddy image:\n{r.stdout[:200]}"
    ls = compose("exec", "-T", "caddy", "ls", "/srv")
    assert ls.returncode == 0
    names = ls.stdout.split()
    assert {"index.html", "200.html", "fonts", "_app"} <= set(names) and not set(DIRS) & set(names), names


# ---------------------------------------------------------------- media-sync (against the live SeaweedFS)

SUMMARY = re.compile(r"media-sync: bucket=media files=(\d+) uploaded=(\d+) \((\d+) B\) unchanged=(\d+) deleted=(\d+) failed=(\d+)")


def run_sync(*args: str) -> dict[str, int]:
    r = compose("run", "--rm", "-T", "media-sync", *args)
    assert r.returncode == 0, r.stdout[-600:] + r.stderr[-600:]
    m = SUMMARY.search(r.stdout)
    assert m, r.stdout[-600:]
    return dict(zip(("files", "uploaded", "bytes", "unchanged", "deleted", "failed"), map(int, m.groups())))


def test_media_sync_is_idempotent():
    s = run_sync()
    assert s["files"] == len(FILES) and s["uploaded"] == 0 and s["deleted"] == 0 and s["failed"] == 0
    assert s["unchanged"] == len(FILES)


def test_media_sync_prunes_objects_missing_from_the_source(web):
    key = "landing/_e2e_prune_tmp.txt"
    put = ("import boto3,os;from botocore.client import Config;"
           "c=boto3.client('s3',endpoint_url=os.environ['MEDIA_S3_ENDPOINT'],aws_access_key_id=os.environ['MEDIA_S3_ACCESS_KEY'],"
           "aws_secret_access_key=os.environ['MEDIA_S3_SECRET_KEY'],region_name='us-east-1',config=Config(s3={'addressing_style':'path'}));"
           f"c.put_object(Bucket='media',Key='{key}',Body=b'temp',ContentType='text/plain')")
    r = compose("run", "--rm", "-T", "--entrypoint", "python", "media-sync", "-c", put)
    assert r.returncode == 0, r.stderr[-400:]
    assert web.get("/" + key).text == "temp"
    pruned = run_sync("--prune")
    assert pruned["files"] == len(FILES) and pruned["deleted"] == 1 and pruned["failed"] == 0
    assert web.get("/" + key).status_code == 404
    assert run_sync()["deleted"] == 0 and web.get("/landing/hero.mp4", headers={"Range": "bytes=0-9"}).status_code == 206


def test_anonymous_s3_cannot_write_or_list():
    code = ("import urllib.request as u,urllib.error as e\n"
            "def st(req):\n"
            "    try: return u.urlopen(req,timeout=10).status\n"
            "    except e.HTTPError as x: return x.code\n"
            "b='http://seaweedfs:8333/media'\n"
            "print('PUT',st(u.Request(b+'/landing/_anon.txt',data=b'x',method='PUT')))\n"
            "print('DELETE',st(u.Request(b+'/landing/hero.mp4',method='DELETE')))\n"
            "print('LIST',st(u.Request(b+'/?list-type=2')))\n"
            "print('GET',st(u.Request(b+'/audio/samples.json')))\n")
    r = compose("run", "--rm", "-T", "--entrypoint", "python", "media-sync", "-c", code)
    got = dict(line.split() for line in r.stdout.split("\n") if line.strip() and line.split()[0] in {"PUT", "DELETE", "LIST", "GET"})
    assert got == {"PUT": "403", "DELETE": "403", "LIST": "403", "GET": "200"}, (got, r.stderr[-300:])


# ---------------------------------------------------------------- media_sync.py logic against a fake S3 (no stack, no boto3)

def _load_script():
    spec = importlib.util.spec_from_file_location("media_sync", ROOT / "scripts" / "media_sync.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ms = _load_script()


class FakeError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class FakeS3:
    """In-memory bucket; list_objects_v2 pages by 2 keys so the continuation-token path is exercised."""

    def __init__(self, bucket_exists: bool = True) -> None:
        self.buckets: dict[str, dict[str, dict]] = {"media": {}} if bucket_exists else {}
        self.puts = 0
        self.fail_keys: set[str] = set()

    def head_bucket(self, Bucket):
        if Bucket not in self.buckets:
            raise FakeError("404")

    def create_bucket(self, Bucket):
        self.buckets[Bucket] = {}

    def list_objects_v2(self, Bucket, ContinuationToken=None):
        keys = sorted(self.buckets[Bucket])
        start = int(ContinuationToken or 0)
        page = keys[start:start + 2]
        out = {"Contents": [{"Key": k, "ETag": f'"{self.buckets[Bucket][k]["etag"]}"', "Size": self.buckets[Bucket][k]["size"]} for k in page],
               "IsTruncated": start + 2 < len(keys)}
        if out["IsTruncated"]:
            out["NextContinuationToken"] = str(start + 2)
        return out

    def head_object(self, Bucket, Key):
        o = self.buckets[Bucket][Key]
        return {"ContentType": o["ContentType"], "CacheControl": o["CacheControl"]}

    def put_object(self, Bucket, Key, Body, ContentType, CacheControl):
        if Key in self.fail_keys:
            raise FakeError("InternalError")
        self.puts += 1
        self.buckets[Bucket][Key] = {"etag": hashlib.md5(Body).hexdigest(), "size": len(Body), "ContentType": ContentType, "CacheControl": CacheControl}

    def delete_object(self, Bucket, Key):
        del self.buckets[Bucket][Key]


@pytest.fixture
def src(tmp_path, monkeypatch):
    monkeypatch.setattr(ms.time, "sleep", lambda _s: None)
    for rel, body in {"landing/a.mp4": b"v" * 10, "landing/b.webp": b"w", "labels/pixel/c.png": b"p" * 5, "audio/d.bin": b"\0\1",
                      "audio/e.json": b"{}", "icons/f.png": b"i", "fonts/ignored.woff": b"f", "other/ignored.txt": b"x"}.items():
        f = tmp_path / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(body)
    return tmp_path


def test_script_uploads_only_the_four_dirs_with_types_and_cache_control(src):
    s3 = FakeS3(bucket_exists=False)
    st = ms.sync(s3, "media", src, log=lambda *_: None)
    assert st["bucket_created"] and st["uploaded"] == st["files"] == 6 and st["failed"] == 0
    assert sorted(s3.buckets["media"]) == ["audio/d.bin", "audio/e.json", "icons/f.png", "labels/pixel/c.png", "landing/a.mp4", "landing/b.webp"]
    o = s3.buckets["media"]
    assert o["landing/a.mp4"]["ContentType"] == "video/mp4" and o["landing/b.webp"]["ContentType"] == "image/webp"
    assert o["audio/d.bin"]["ContentType"] == "application/octet-stream" and o["audio/e.json"]["ContentType"] == "application/json"
    assert all(v["CacheControl"] == "public, max-age=86400" for v in o.values())


def test_script_second_run_uploads_nothing_and_changes_are_picked_up(src):
    s3 = FakeS3()
    ms.sync(s3, "media", src, log=lambda *_: None)
    puts = s3.puts
    again = ms.sync(s3, "media", src, log=lambda *_: None)
    assert again["uploaded"] == 0 and again["skipped"] == 6 and s3.puts == puts
    (src / "landing" / "a.mp4").write_bytes(b"changed!")  # content change
    s3.buckets["media"]["audio/e.json"]["CacheControl"] = "no-store"  # header drift
    third = ms.sync(s3, "media", src, log=lambda *_: None)
    assert third["uploaded"] == 2 and third["skipped"] == 4
    assert s3.buckets["media"]["audio/e.json"]["CacheControl"] == "public, max-age=86400"


def test_script_prune_only_when_asked_and_never_on_an_empty_source(src, tmp_path_factory):
    s3 = FakeS3()
    ms.sync(s3, "media", src, log=lambda *_: None)
    s3.buckets["media"]["landing/old.mp4"] = {"etag": "x", "size": 1, "ContentType": "video/mp4", "CacheControl": "x"}
    s3.buckets["media"]["labels/gone.png"] = {"etag": "x", "size": 1, "ContentType": "image/png", "CacheControl": "x"}
    assert ms.sync(s3, "media", src, log=lambda *_: None)["deleted"] == 0 and "landing/old.mp4" in s3.buckets["media"]
    st = ms.sync(s3, "media", src, prune=True, log=lambda *_: None)
    assert st["deleted"] == 2 and "landing/old.mp4" not in s3.buckets["media"] and len(s3.buckets["media"]) == 6
    with pytest.raises(SystemExit):  # a missing submodule must not wipe the bucket
        ms.sync(s3, "media", tmp_path_factory.mktemp("empty"), prune=True, log=lambda *_: None)
    assert len(s3.buckets["media"]) == 6


def test_script_counts_failures_and_main_exits_non_zero(src, monkeypatch):
    s3 = FakeS3()
    s3.fail_keys = {"audio/d.bin"}
    st = ms.sync(s3, "media", src, log=lambda *_: None)
    assert st["failed"] == 1 and st["uploaded"] == 5
    monkeypatch.setattr(ms, "make_client", lambda *a: s3)
    monkeypatch.setenv("MEDIA_S3_ACCESS_KEY", "k")
    monkeypatch.setenv("MEDIA_S3_SECRET_KEY", "s")
    assert ms.main(["--source", str(src)]) == 1
    s3.fail_keys.clear()
    assert ms.main(["--source", str(src)]) == 0
    monkeypatch.delenv("MEDIA_S3_SECRET_KEY")
    assert ms.main(["--source", str(src)]) == 2
