"""Operations API (/api/ops/*) and the live ops event stream (/api/stream/ops).

The six /api/ops/* routes are thin reads over ava_bridge/dashboard.py — summary,
schedule, services, tools, connectors, alerts.

/api/stream/ops is the Server-Sent Events feed the Operations page subscribes
to, and it moves here WHOLE rather than being split from the routes it serves.
It is a single long-lived async generator emitting several event types on one
connection (turn.update, hardware.tick, device.event, alert.raise /
alert.clear, plus a keepalive), and every one of those is the streaming form of
something the /api/ops/* reads return. Separating the poll endpoints from the
push channel would put two halves of one contract in two files, where a new
event type could be added to one and forgotten in the other.
"""
import asyncio
import json
import time

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from . import dashboard, devices

router = APIRouter()
@router.get("/api/ops/summary")
async def api_ops_summary():
    return await run_in_threadpool(dashboard.ops_summary)

@router.get("/api/ops/schedule")
async def api_ops_schedule():
    return await run_in_threadpool(dashboard.ops_schedule)

@router.get("/api/ops/services")
async def api_ops_services():
    return await run_in_threadpool(dashboard.ops_services)

@router.get("/api/ops/tools")
async def api_ops_tools(limit: int = 15):
    return await run_in_threadpool(dashboard.ops_tools, limit)

@router.get("/api/ops/connectors")
async def api_ops_connectors():
    return await run_in_threadpool(dashboard.connectors_info)

@router.get("/api/ops/alerts")
async def api_ops_alerts():
    return await run_in_threadpool(dashboard.ops_alerts)

@router.get("/api/stream/ops")
async def api_stream_ops(request: Request):
    """Server-Sent Events: live turn/hardware/alert deltas for the Operations
    tab. A 1 Hz producer snapshots state, diffs it, and emits only changes; alerts
    re-evaluate every ~5s; a heartbeat keeps the connection alive."""
    async def gen():
        last_turns: dict = {}
        last_alerts: set = set()
        last_beat = time.time()
        tick = 0
        # Only stream device events that arrive AFTER this client connects (don't
        # replay history into a fresh toast storm).
        last_dev_seq = devices.current_seq()
        # Prime clients with the current alert set on connect.
        try:
            init = await run_in_threadpool(dashboard.ops_alerts)
            yield f"event: alert.state\ndata: {json.dumps(init.get('active', []))}\n\n"
        except Exception:  # noqa: BLE001
            pass
        while True:
            if await request.is_disconnected():
                break
            try:
                snap = await run_in_threadpool(dashboard.live_snapshot)
            except Exception:  # noqa: BLE001
                await asyncio.sleep(1.0)
                continue

            for tid, cur in snap["turns"].items():
                if last_turns.get(tid) != cur:
                    yield f"event: turn.update\ndata: {json.dumps({'id': tid, **cur})}\n\n"
            last_turns = snap["turns"]

            if snap.get("hw"):
                yield f"event: hardware.tick\ndata: {json.dumps(snap['hw'])}\n\n"

            # Live device events pushed by connector apps (ava_bridge/devices.py).
            last_dev_seq, new_events = devices.live_since(last_dev_seq)
            for ev in new_events:
                yield f"event: device.event\ndata: {json.dumps(ev)}\n\n"

            # Alerts every ~5 ticks (5s).
            if tick % 5 == 0:
                try:
                    res = await run_in_threadpool(dashboard.ops_alerts)
                    active = res.get("active", [])
                    ids = {a["id"] for a in active}
                    for a in active:
                        if a["id"] not in last_alerts:
                            yield f"event: alert.raise\ndata: {json.dumps(a)}\n\n"
                    for gone in last_alerts - ids:
                        yield f"event: alert.clear\ndata: {json.dumps({'id': gone})}\n\n"
                    last_alerts = ids
                except Exception:  # noqa: BLE001
                    pass

            now = time.time()
            if now - last_beat > 15:
                last_beat = now
                yield ": keepalive\n\n"

            tick += 1
            await asyncio.sleep(1.0)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})

