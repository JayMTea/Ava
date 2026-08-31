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
import re

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import config
from . import provision as provision_mod
from .runtime import nemoclaw, nemoclaw_registry

app = FastAPI(title="ava-agent-runtime")

#: Same shape ava_bridge/hub/connectors.py enforces when a connector is created,
#: checked again at the door: this value crosses a network boundary and becomes a
#: filename component inside the sandbox. install.sh checks it a third time — the
#: two ends of an exec are worth two checks.
_CONNECTOR_ID = re.compile(r"^[a-z][a-z0-9_-]{1,31}$")
_rt = nemoclaw()

# What this container can do, advertised on /healthz so the bridge's
# RemoteRuntime can refuse to ask for something an older build would silently
# mishandle. Additive only: dropping an entry is a breaking change.
#
# `health.model` says this shim can NAME the model its sandbox is running, on
# /healthz. It is what lets the bridge tell two states apart that otherwise
# arrive identically — as an absent `model` key — and that have completely
# different fixes:
#
#   advertised, model present  -> the brain is that model
#   advertised, model absent   -> the sandbox genuinely has no model onboarded
#   NOT advertised             -> this agent container predates the field and
#                                 cannot answer; rebuild it
#
# Without the flag the bridge can only say "no model", which is the sentence
# that sent an owner to Setup to choose a model that was already chosen.
CAPABILITIES = ["provision.scope", "provision.assert", "provision.connector",
                "health.model"]


@app.middleware("http")
async def _auth(request: Request, call_next):
    if request.url.path != "/healthz":
        tok = request.headers.get("x-ava-agent-token", "")
        if not (config.AGENT_TOKEN and hmac.compare_digest(tok, config.AGENT_TOKEN)):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
    return await call_next(request)


def _brain_fields() -> dict:
    """`{model, provider}` for the sandbox this shim drives, or `{}`.

    THE HALF OF A CONTRACT THAT WAS NEVER BUILT. `RemoteRuntime.available()`
    reads `model` and `provider` straight off this response into the cache that
    `sandbox_info()` serves, and `models.effective_brain()` calls `sandbox_info`
    to answer "what is Ava thinking with". This route never sent either key, so
    on every `remote` install the model id resolved EMPTY and /api/health, the
    chat header, Setup -> Agent and the hardware panel all announced "No model
    is configured, so there is nothing to answer with" — while the sandbox held
    a correctly onboarded model. Neither side was wrong on its own. Nothing
    tested the pair, which is what tests/test_remote_brain_contract.py now does.

    Read from the NemoClaw REGISTRY, not from `_rt.sandbox_info()`, and that
    choice is the reason this is a helper instead of one more line in `healthz`:

      * `NemoClawRuntime.sandbox_info(wait=True)` shells out to `nemoclaw list
        --json` on a 15s timeout. The bridge polls /healthz every 15s and the
        PUBLIC /api/health sits behind it, so a shell-out here is a health probe
        that can outlive the request that asked for it.
      * `sandbox_info(wait=False)` serves a 120s cache and refreshes in a
        background thread, so the FIRST caller after a restart gets None. That
        is the same "empty is self-correcting" assumption that turned out to be
        permanent on openclaw_gw, and it would make the brain appear only on the
        second probe — an install that names itself on a delay, intermittently.
      * `nemoclaw_registry.registry_record()` reads ~/.nemoclaw/sandboxes.json
        directly behind a 30s cache. It is the source `openclaw_gw.sandbox_info`
        already trusts for exactly this question, it costs a stat, and it is
        correct on the very first call.

    Returns `{}` rather than `{"model": ""}` when the registry cannot answer, so
    "we could not look" never hardens into the claim that the sandbox has no
    model. `available()` treats an absent key and an empty string identically,
    but they are different sentences to whoever reads this response next, and
    `health.model` in CAPABILITIES is what tells them apart.

    Never raises. A health probe that can fail is not a health probe.
    """
    try:
        rec = nemoclaw_registry.registry_record(config.OC_SANDBOX) or {}
        model = str(rec.get("model") or "").strip()
        provider = str(rec.get("provider") or "").strip()
    except Exception:  # noqa: BLE001 — health must never fail on a probe
        return {}
    out = {}
    if model:
        out["model"] = model
    # Only alongside a model. `effective_brain` uses provider as the ENGINE it
    # displays, and a provider with no model to attach it to is a label for
    # nothing — it would render an engine name beside a blank brain.
    if model and provider:
        out["provider"] = provider
    return out


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
    # `model`/`provider` ride along on the probe the bridge already makes every
    # 15s. That is what keeps `RemoteRuntime.sandbox_info()` free and lets the
    # public /api/health name the brain without paying a second round trip —
    # the same reason `capabilities` is carried here rather than on its own
    # route. Merged rather than inlined so an unanswerable registry contributes
    # no keys at all instead of empty ones.
    body.update(_brain_fields())
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
    if provision_mod.parse_scope(scope) is None:
        return JSONResponse(
            {"ok": False, "error": f"unsupported scope {scope!r}",
             "error_code": "unknown_scope",
             "supported": list(provision_mod.ALL_SCOPES)}, status_code=400)
    connector = str(body.get("connector") or "").strip() or None
    if connector and not _CONNECTOR_ID.match(connector):
        return JSONResponse(
            {"ok": False, "error": f"not a connector id: {connector!r}",
             "error_code": "bad_connector"}, status_code=400)
    return _rt.provision(auto_install=bool(body.get("auto_install")), scope=scope,
                         connector=connector)


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
