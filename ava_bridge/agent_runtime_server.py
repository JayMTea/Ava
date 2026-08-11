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
from . import provision as provision_mod
from .runtime import nemoclaw

app = FastAPI(title="ava-agent-runtime")
_rt = nemoclaw()

# What this container can do, advertised on /healthz so the bridge's
# RemoteRuntime can refuse to ask for something an older build would silently
# mishandle. Additive only: dropping an entry is a breaking change.
CAPABILITIES = ["provision.scope", "provision.assert"]


@app.middleware("http")
async def _auth(request: Request, call_next):
    if request.url.path != "/healthz":
        tok = request.headers.get("x-ava-agent-token", "")
        if not (config.AGENT_TOKEN and hmac.compare_digest(tok, config.AGENT_TOKEN)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


@app.get("/healthz")
def healthz(request: Request):
    # `ready` gates the bridge's RemoteRuntime.available(): true only once the
    # sandbox exists and the CLI resolves (onboarding finished).
    #
    # `authed` exists because this is the ONE unauthenticated route, so a token
    # that does not match between the two containers — the single easiest mistake
    # in the Docker full-agent profile — used to leave the bridge reporting the
    # agent as available while every real call came back 401. The owner saw a
    # healthy agent and failing turns, with nothing connecting the two.
    #
    # Only present when a token was actually offered, so an anonymous prober
    # learns nothing it could not learn by making a request and reading the 401.
    body = {"ok": True, "ready": _rt.available(), "capabilities": CAPABILITIES}
    tok = request.headers.get("x-ava-agent-token", "")
    if tok:
        body["authed"] = bool(config.AGENT_TOKEN
                              and hmac.compare_digest(tok, config.AGENT_TOKEN))
    return body


@app.get("/status")
def status():
    return _rt.status()


@app.post("/run_turn")
async def run_turn(request: Request):
    """Proxy one turn into the sandbox.

    Answers the FAILURE as a body rather than letting it become a 500.
    `NemoClawRuntime.run_turn` raises RuntimeError on unparsable output and
    TimeoutExpired after OC_TIMEOUT; both used to reach `RemoteRuntime._post`'s
    `raise_for_status()`, which is the one method on that adapter that does not
    swallow — so `turns.py` rendered its canned "my tools timed out or hit a
    snag" and a 401 from a token mismatch looked exactly like a slow tool. The
    owner could not tell a misconfigured deployment from a busy one.
    """
    body = await request.json()
    try:
        reply, tools = _rt.run_turn(body.get("text", ""),
                                    session_id=body.get("session_id"))
    except Exception as e:  # noqa: BLE001 — the reason IS the payload
        return JSONResponse({"error": f"{type(e).__name__}: {e}"[:400],
                             "error_code": "agent_turn_failed"}, status_code=200)
    return {"reply": reply, "tools": tools}


#: Ceiling for a caller-supplied exec timeout. `int(body["timeout"])` was
#: unvalidated and unbounded, so a bad value was either an unhandled ValueError
#: (a 500 from a malformed request) or a request that pinned a worker for as long
#: as it liked. Live chain-of-thought reads a file; it does not need minutes.
_EXEC_TIMEOUT_MAX = 120


@app.post("/exec")
async def exec_(request: Request):
    body = await request.json()
    try:
        timeout = int(body.get("timeout", 20))
    except (TypeError, ValueError):
        timeout = 20
    timeout = max(1, min(timeout, _EXEC_TIMEOUT_MAX))
    try:
        out = _rt.exec(body.get("inner", ""), timeout=timeout)
    except Exception as e:  # noqa: BLE001 — RemoteRuntime.exec swallows anyway,
        return {"out": "", "error": f"{type(e).__name__}: {e}"[:200]}
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
    scope = str(body.get("scope") or "all")
    # 400 rather than a silent fall-through to `all`. This route reads its body
    # with .get() and ignores unknown keys, so a newer bridge asking an older
    # container for a persona-only apply used to get a full ten-minute redeploy
    # reported as success. Refusing a scope we do not know is the other half of
    # the /healthz capability handshake.
    if scope not in provision_mod.ALL_SCOPES:
        return JSONResponse(
            {"ok": False, "error": f"unsupported scope {scope!r}",
             "error_code": "unknown_scope",
             "supported": list(provision_mod.ALL_SCOPES)}, status_code=400)
    return _rt.provision(auto_install=bool(body.get("auto_install")), scope=scope)


@app.post("/registry_record")
async def registry_record():
    """The sandbox's NemoClaw registry entry, for the bridge's drift report.

    The bridge container cannot read ~/.nemoclaw itself — the CLI, the Docker
    socket and that registry all live out here.
    """
    return {"record": _rt.registry_record()}


def main() -> int:
    import uvicorn
    host = os.environ.get("AVA_AGENT_BIND", "0.0.0.0")
    port = int(os.environ.get("AVA_AGENT_PORT", "9100"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
