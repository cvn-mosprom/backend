"""An uncached regional probe. No authentication, DB or Redis access."""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from ..observability import CLIENT_RTT, ROUTE_SELECTIONS
from ..ratelimit import rate_limit

router = APIRouter(prefix="/routing", tags=["routing"])


@router.get("/ping")
async def ping(request: Request) -> JSONResponse:
    cfg = request.app.state.settings
    return JSONResponse(
        {"regionId": cfg.region_id, "releaseId": cfg.release_id, "apiVersion": 1},
        headers={"Cache-Control": "no-store", "Pragma": "no-cache"},
    )


class RouteReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    regionId: str = Field(pattern=r"^[a-z0-9-]{1,32}$")
    rttMs: float | None = Field(default=None, ge=0, le=10000, allow_inf_nan=False)


@router.post("/report", dependencies=[Depends(rate_limit("routing_report"))])
async def report(body: RouteReport):
    # Fixed label set prevents arbitrary clients from creating unbounded time series.
    region = body.regionId if body.regionId in {"msk-1", "kzn", "spb", "msk-2", "local", "load-balancer"} else "other"
    ROUTE_SELECTIONS.labels(region).inc()
    if body.rttMs is not None:
        CLIENT_RTT.labels(region).observe(body.rttMs / 1000)
    return {"accepted": True}
