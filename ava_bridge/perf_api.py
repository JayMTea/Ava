"""Telemetry reads (/api/perf/*, /api/hardware*) — cookie-gated.

Everything the Vitals dashboard polls: the perf summary, time series, recent
rows and cost rollup, plus the current hardware snapshot and its history. All
six are thin reads over ava_bridge/dashboard.py and ava_bridge/hardware.py — no
writes, no state of their own.

The push counterpart lives in ops_api.py: /api/stream/ops emits hardware.tick
from the same source these read. If you add a field here, check whether the
stream should carry it too.
"""
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from . import dashboard, hardware

router = APIRouter()
@router.get("/api/hardware")
async def api_hardware():
    """Live hardware snapshot for the app's floating monitor: GPU
    utilisation/temperature, unified/system memory used/free, and CPU
    utilisation. Auth-gated like every other /api route."""
    return await run_in_threadpool(hardware.stats)

@router.get("/api/perf/summary")
async def api_perf_summary(app: str | None = None, category: str | None = None,
                           since: str | None = None):
    return await run_in_threadpool(dashboard.perf_summary, app, category, since)

@router.get("/api/perf/series")
async def api_perf_series(metric: str = "tokens_per_sec", bucket: str = "1h",
                          since: str = "24h", app: str | None = None,
                          category: str | None = None):
    return await run_in_threadpool(dashboard.perf_series, metric, bucket, since,
                                   app, category)

@router.get("/api/perf/recent")
async def api_perf_recent(limit: int = 50, app: str | None = None,
                          category: str | None = None):
    return await run_in_threadpool(dashboard.perf_recent, limit, app, category)

@router.get("/api/perf/cost")
async def api_perf_cost(since: str = "7d", group: str = "model"):
    return await run_in_threadpool(dashboard.perf_cost, since, group)

@router.get("/api/hardware/history")
async def api_hardware_history(since: str = "1d", bucket: str = "5m"):
    return {"ok": True, "samples": await run_in_threadpool(hardware.history_series, since, bucket)}

