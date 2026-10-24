"""Low-cardinality Prometheus metrics; no user identifiers or request bodies."""
from __future__ import annotations

import os
import time

from prometheus_client import CollectorRegistry, Counter, Histogram, generate_latest, multiprocess
from starlette.responses import Response

REQUESTS = Counter("mosprom_http_requests_total", "HTTP requests", ["method", "route", "status"])
LATENCY = Histogram("mosprom_http_duration_seconds", "HTTP duration", ["method", "route"])
ROUTE_SELECTIONS = Counter("mosprom_client_route_selections_total", "Anonymous route selections", ["region"])
CLIENT_RTT = Histogram("mosprom_client_probe_seconds", "Client-observed HTTPS latency", ["region"])


class MetricsMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        status = 500
        start = time.monotonic()

        async def measured(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, measured)
        finally:
            if scope.get("path") not in {"/metrics", "/routing/ping", "/health", "/ready"}:
                route = getattr(scope.get("route"), "path", "unmatched")
                REQUESTS.labels(scope["method"], route, str(status)).inc()
                LATENCY.labels(scope["method"], route).observe(time.monotonic() - start)


async def metrics():
    registry = None
    if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
        registry = CollectorRegistry()
        multiprocess.MultiProcessCollector(registry)
    return Response(generate_latest(registry) if registry else generate_latest(), media_type="text/plain; version=0.0.4")
