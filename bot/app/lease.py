"""etcd v3 lease. Polling and scheduler run only while this process owns the key."""
from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import ssl
import uuid

import httpx


def b64(value: str) -> str:
    return base64.b64encode(value.encode()).decode()


class Lease:
    def __init__(self):
        self.endpoints = [s.rstrip("/") for s in os.environ["ETCD_ENDPOINTS"].split(",")]
        if any(not endpoint.startswith("https://") for endpoint in self.endpoints):
            raise ValueError("ETCD_ENDPOINTS must use HTTPS")
        context = ssl.create_default_context(cafile=os.environ["ETCD_CA_FILE"])
        context.load_cert_chain(os.environ["ETCD_CERT_FILE"], os.environ["ETCD_KEY_FILE"])
        self.client = httpx.AsyncClient(verify=context, timeout=3, follow_redirects=False)
        self.key = b64(os.environ.get("BOT_LEASE_KEY", "/mosprom/prod/bot"))
        self.owner = b64(str(uuid.uuid4()))
        self.lease_id: str | None = None

    async def post(self, path, body):
        last = None
        for endpoint in self.endpoints:
            try:
                response = await self.client.post(endpoint + path, json=body)
                response.raise_for_status()
                return response.json()
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
        raise RuntimeError("etcd unavailable") from last

    async def acquire(self) -> bool:
        result = await self.post("/v3/lease/grant", {"TTL": 15})
        self.lease_id = str(result["ID"])
        result = await self.post("/v3/kv/txn", {
            "compare": [{"key": self.key, "target": "VERSION", "version": "0", "result": "EQUAL"}],
            "success": [{"request_put": {"key": self.key, "value": self.owner, "lease": self.lease_id}}],
            "failure": [],
        })
        if not result.get("succeeded"):
            await self.release()
            return False
        return True

    async def renew(self) -> None:
        # The gateway is a stream: reading the whole response would wait forever.
        result = None
        for endpoint in self.endpoints:
            try:
                async with self.client.stream("POST", endpoint + "/v3/lease/keepalive", json={"ID": self.lease_id}) as response:
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        if line:
                            result = json.loads(line).get("result", {})
                            break
                if result and int(result.get("TTL", 0)) > 0:
                    break
            except (httpx.HTTPError, ValueError):
                continue
        if not result or int(result.get("TTL", 0)) <= 0:
            raise RuntimeError("bot lease lost")
        owned = await self.post("/v3/kv/txn", {
            "compare": [{"key": self.key, "target": "VALUE", "value": self.owner, "result": "EQUAL"}],
            "success": [], "failure": [],
        })
        if not owned.get("succeeded"):
            raise RuntimeError("bot ownership lost")

    async def release(self):
        if self.lease_id:
            try:
                await self.post("/v3/lease/revoke", {"ID": self.lease_id})
            except Exception:
                pass  # TTL expires even if the network is unavailable.
            self.lease_id = None


async def run_with_lease(run):
    lease = Lease()
    try:
        while True:
            try:
                acquired = await asyncio.wait_for(lease.acquire(), timeout=6)
            except (RuntimeError, httpx.HTTPError, TimeoutError):
                acquired = False
            if not acquired:
                await asyncio.sleep(2 + random.random())
                continue
            stop = asyncio.Event()
            task = asyncio.create_task(run(stop))
            try:
                while not task.done():
                    await asyncio.sleep(3)
                    await asyncio.wait_for(lease.renew(), timeout=4)
                return task.result()
            finally:
                # Cancellation also stops an in-flight Telegram send and queued scheduler jobs.
                stop.set()
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                await lease.release()
    finally:
        await lease.client.aclose()
