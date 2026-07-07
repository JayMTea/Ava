"""Agent-runtime shim — exposes NemoClawRuntime over HTTP for RemoteRuntime.

Runs INSIDE the agent-runtime container (which owns the nemoclaw CLI + the
Docker socket). The bridge container's `RemoteRuntime` calls these endpoints, so
the whole agent surface (run_turn / exec / session_file / provision / status)
works across the network exactly as it does in-process. Every route is guarded
by the shared bearer (X-Ava-Agent-Token == config.AGENT_TOKEN), so only the
bridge — which mounts the same /data secret — can drive it.

Run:  uvicorn ava_bridge.agent_runtime_server:app --host 0.0.0.0 --port 9100
(the container entrypoint onboards + provisions the sandbox first).
"""
from __future__ import annotations

import hmac
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from .runtime import nemoclaw

app = FastAPI(title="ava-agent-runtime")
_rt = nemoclaw()


@app.middleware("http")
async def _auth(request: Request, call_next):
    if request.url.path != "/healthz":
        tok = request.headers.get("x-ava-agent-token", "")
        if not (config.AGENT_TOKEN and hmac.compare_digest(tok, config.AGENT_TOKEN)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/healthz")
def healthz():
    # `ready` gates the bridge's RemoteRuntime.available(): true only once the
    # sandbox exists and the CLI resolves (onboarding finished).
    return {"ok": True, "ready": _rt.available()}


@app.get("/status")
def status():
    return _rt.status()


@app.post("/run_turn")
async def run_turn(request: Request):
    body = await request.json()
    reply, tools = _rt.run_turn(body.get("text", ""),
                                session_id=body.get("session_id"))
    return {"reply": reply, "tools": tools}


@app.post("/exec")
async def exec_(request: Request):
    body = await request.json()
    out = _rt.exec(body.get("inner", ""), timeout=int(body.get("timeout", 20)))
    return {"out": out}


@app.post("/session_file")
async def session_file(request: Request):
    body = await request.json()
    return {"path": _rt.session_file(body.get("session_id", ""))}


@app.post("/discard_session")
async def discard_session(request: Request):
    body = await request.json()
    return {"ok": _rt.discard_session(body.get("session_id", ""))}


@app.post("/warm")
def warm():
    _rt.warm()
    return {"ok": True}


@app.post("/provision")
async def provision(request: Request):
    body = await request.json()
    return _rt.provision(auto_install=bool(body.get("auto_install")))


def main() -> int:
    import uvicorn
    host = os.environ.get("AVA_AGENT_BIND", "0.0.0.0")
    port = int(os.environ.get("AVA_AGENT_PORT", "9100"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
