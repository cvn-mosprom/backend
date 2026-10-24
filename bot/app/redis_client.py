"""Redis direct/Sentinel configuration shared by bot runtime consumers."""
import os

import redis.asyncio as redis
from redis.asyncio.sentinel import Sentinel


def create_redis(url: str):
    nodes = os.environ.get("REDIS_SENTINEL_NODES", "")
    options = {"decode_responses": True, "socket_connect_timeout": 2, "socket_timeout": 3}
    if not nodes:
        return redis.from_url(url, **options)
    addresses = [(host, int(port)) for host, port in (node.strip().rsplit(":", 1) for node in nodes.split(","))]
    tls = os.environ.get("REDIS_TLS") == "1"
    security = {"ssl": True, "ssl_ca_certs": os.environ["REDIS_CA_FILE"]} if tls else {}
    sentinel = Sentinel(addresses, sentinel_kwargs={**security, "password": os.environ.get("REDIS_SENTINEL_PASSWORD"), "socket_timeout": 2}, **options)
    return sentinel.master_for(os.environ.get("REDIS_MASTER_NAME", "mosprom"), password=os.environ.get("REDIS_PASSWORD"), **security)
