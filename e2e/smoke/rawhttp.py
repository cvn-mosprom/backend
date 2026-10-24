"""Raw TLS sockets to the stack: the clients a real flaky network produces (aborted before the response, truncated body)."""

import json
import socket
import ssl
import time
from urllib.parse import urlsplit

from conftest import BASE

_u = urlsplit(BASE)
HOST = _u.hostname or "localhost"
PORT = _u.port or (443 if _u.scheme == "https" else 80)


def _connect() -> socket.socket:
    s = socket.create_connection((HOST, PORT), timeout=10)
    if _u.scheme == "https":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(s, server_hostname=HOST)
    return s


def request_bytes(method: str, path: str, tg_id: int, body: bytes = b"", content_length: int | None = None, name: str = "raw") -> bytes:
    head = [f"{method} {path} HTTP/1.1", f"Host: {HOST}", f"Authorization: dev {tg_id}:{name}", "Content-Type: application/json",
            f"Content-Length: {len(body) if content_length is None else content_length}", "Connection: close", "", ""]
    return "\r\n".join(head).encode() + body


def fire_and_abort(raw: bytes, linger_s: float = 0.0) -> None:
    """Send the whole request and hang up without reading the answer (the 'lost response')."""
    s = _connect()
    try:
        s.sendall(raw)
        time.sleep(linger_s)
    finally:
        try:
            s.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        s.close()


def exchange(raw: bytes, read_timeout_s: float = 8.0) -> tuple[int | None, bytes]:
    """Send `raw`, read until the peer closes or `read_timeout_s`; (status or None, body)."""
    s = _connect()
    s.settimeout(read_timeout_s)
    chunks = []
    try:
        s.sendall(raw)
        while True:
            try:
                data = s.recv(65536)
            except (TimeoutError, ssl.SSLError, ConnectionError):
                break
            if not data:
                break
            chunks.append(data)
    finally:
        s.close()
    blob = b"".join(chunks)
    status = int(blob.split(b" ", 2)[1]) if blob.startswith(b"HTTP/") else None
    return status, blob.partition(b"\r\n\r\n")[2]


def json_body(**fields) -> bytes:
    return json.dumps(fields).encode()
