"""The live hardware snapshot (/api/hardware) — cookie-gated.

This module used to carry everything the Vitals dashboard polled: the perf
summary, time series, recent rows, cost rollup and hardware history. Those went
with the page. What remains is the one read that outlived it — the snapshot the
floating hardware monitor polls on every view, which `ava doctor` and the setup
wizard also call through `hardware.stats`.

The perf LOGS this module used to read are untouched: `perf_log.py` still writes
one record per generation, `perf_store.py` still rolls them up, and both the
agent's `read_performance` tool (via /internal/perf) and the warehouse shipper
sidecar still consume them. Only the browser-facing reads are gone.
"""
from fastapi import APIRouter
from starlette.concurrency import run_in_threadpool

from . import hardware

router = APIRouter()


@router.get("/api/hardware")
async def api_hardware():
    """Live hardware snapshot for the app's floating monitor: GPU
    utilisation/temperature, unified/system memory used/free, and CPU
    utilisation. Auth-gated like every other /api route."""
    return await run_in_threadpool(hardware.stats)
