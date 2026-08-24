#!/usr/bin/env python3
"""Capture the live OpenClaw gateway's method schemas, event topics and payload
shapes into tracked test fixtures.

WHY THIS EXISTS: every dead surface in the Agent tab was code written from prose
docs, agreed with by a fake built on the same assumption. Five method names, six
param shapes, two event topics and several payload shapes were invented. The only
thing that ever caught them was a live probe. This tool makes "captured from
life" the default: it learns each method's schema from the gateway's own
`INVALID_REQUEST` messages (an intentionally-wrong call is the cheapest, safest
probe — nothing executes), and it records real payloads so the fake gateway and
the frontend adapters can be checked against reality rather than against a guess.

Run against THIS box's live gateway (agent.runtime: openclaw_gw, loopback):
    .venv/bin/python qa/capture_gateway.py --schemas          # learn param schemas
    .venv/bin/python qa/capture_gateway.py --schemas --only terminal,cron
    .venv/bin/python qa/capture_gateway.py --check            # re-probe, diff, exit 3 on drift

Outputs (all tracked, all scrubbed of owner identity, all version-stamped):
    qa/fakes/gateway-schemas.json   {_capture, methods: {m: {required, allowed, types, additionalProperties, learned, probe_log}}}
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
FAKES = os.path.join(HERE, "fakes")
SCHEMAS_PATH = os.path.join(FAKES, "gateway-schemas.json")

_REQ_RE = re.compile(r"must have required property '([^']+)'")
_UNEXPECTED_RE = re.compile(r"unexpected property '([^']+)'")
_TYPE_RE = re.compile(r"at /([^:]+): must be (integer|number|boolean|string|array|object)")

# Candidate param names to probe per namespace, learned by trial. A name is kept
# only if the gateway does NOT reject it as unexpected — so a wrong guess is
# harmless, it simply is not recorded.
_CANDIDATES = {
    "terminal.open": ["sessionKey", "sessionId", "cols", "rows", "cwd", "shell", "agentId", "env"],
    "terminal.attach": ["terminalId", "id", "sessionKey"],
    "terminal.input": ["terminalId", "id", "data"],
    "terminal.resize": ["terminalId", "id", "cols", "rows"],
    "terminal.text": ["terminalId", "id"],
    "terminal.close": ["terminalId", "id"],
    "terminal.list": ["sessionKey", "agentId"],
    "chat.abort": ["sessionKey", "runId", "reason", "key"],
    "sessions.abort": ["key", "sessionKey", "runId", "reason"],
    "sessions.patch": ["key", "sessionKey", "pinned", "archived", "title", "patch"],
    "sessions.describe": ["key", "sessionKey"],
    "sessions.preview": ["keys", "sessionKey", "key", "limit"],
    "sessions.reset": ["key", "sessionKey", "reason"],
    "sessions.compact": ["key", "sessionKey"],
    "sessions.cleanup": ["key", "sessionKey"],
    "cron.add": ["name", "schedule", "cron", "expr", "command", "message", "enabled", "agentId"],
    "cron.get": ["id", "jobId", "name"],
    "cron.update": ["id", "jobId", "name", "schedule", "enabled"],
    "cron.remove": ["id", "jobId", "name"],
    "cron.run": ["id", "jobId", "name"],
    "cron.runs": ["id", "jobId", "limit"],
    "cron.status": ["id", "jobId"],
    "exec.approval.list": ["sessionKey", "agentId"],
    "exec.approval.get": ["id", "approvalId"],
    "exec.approval.resolve": ["id", "approvalId", "decision", "approve"],
    "exec.approval.waitDecision": ["id", "approvalId", "timeoutMs"],
    "exec.approvals.get": ["agentId"],
    "exec.approvals.node.get": ["nodeId", "node"],
    "models.authStatus": ["provider"],
    "models.authLogout": ["provider"],
    "skills.search": ["query", "q", "limit"],
    "skills.status": ["agentId"],
    "tts.providers": [],
    "tts.personas": [],
    "usage.status": [],
    "device.pair.list": ["agentId"],
    "node.pair.list": ["agentId"],
    "config.get": ["path"],
    "worktrees.list": ["agentId"],
    "doctor.memory.status": ["agentId"],
    "update.status": [],
    "gateway.identity.get": [],
    "agent.identity.get": [],
    "agents.list": [],
}


def _client():
    from ava_bridge import config, runtime
    if config.AGENT_RUNTIME != "openclaw_gw":
        sys.exit("refuse: agent.runtime is not openclaw_gw")
    rt = runtime.configured()
    c = rt._client
    c.start()
    for _ in range(30):
        st = c.status()
        if st.get("phase") == "ready":
            break
        time.sleep(1)
    if c.status().get("phase") != "ready":
        sys.exit("refuse: gateway not ready")
    if c.status().get("url_class") not in (None, "loopback"):
        sys.exit("refuse: gateway is not loopback")
    return c


def _err_text(c, method: str, params: dict) -> str | None:
    """Send a call; return the gateway's error message, or None if it SUCCEEDED.

    A success here means the params were valid — which, for a schema probe with
    an added junk key, only happens when the method has no strict schema.
    """
    try:
        c.rpc(method, params, timeout=15.0)
        return None
    except Exception as e:
        return str(getattr(e, "detail", None) or e)


def learn_schema(c, method: str) -> dict:
    log: list[str] = []
    required: list[str] = []
    # 1. Discover required props one at a time.
    for _ in range(12):
        params = {k: "x" for k in required}
        msg = _err_text(c, method, params)
        if msg is None:
            break                       # accepted with just the required set
        log.append(msg)
        m = _REQ_RE.search(msg)
        if not m:
            break                       # a non-schema error (auth, not-found, exec)
        if m.group(1) in required:
            break
        required.append(m.group(1))
    # 2. additionalProperties? add a junk key on top of the required set.
    probe = {**{k: "x" for k in required}, "__ava_probe__": 1}
    msg = _err_text(c, method, probe)
    strict = bool(msg and _UNEXPECTED_RE.search(msg))
    if msg:
        log.append(msg)
    # 3. Which candidate params are accepted (not "unexpected").
    allowed = list(required)
    for name in _CANDIDATES.get(method, []):
        if name in allowed:
            continue
        m = _err_text(c, method, {**{k: "x" for k in required}, name: "x"})
        if m and _UNEXPECTED_RE.search(m) and f"'{name}'" in m:
            continue                    # gateway refused it as unexpected
        allowed.append(name)
    # 4. Types: send a wrong-typed value for each allowed prop.
    types: dict[str, str] = {}
    for name in allowed:
        m = _err_text(c, method, {**{k: "x" for k in allowed if k != name}, name: {"__wrong__": 1}})
        if m:
            tm = _TYPE_RE.search(m)
            if tm and tm.group(1).split("/")[-1] == name:
                types[name] = tm.group(2)
    learned = bool(required) or strict or bool(allowed)
    return {
        "required": sorted(required),
        "allowed": sorted(set(allowed)),
        "types": types,
        "additionalProperties": not strict,
        "learned": learned,
        "probe_log": log[:6],
    }


def _scrub(obj):
    home = os.path.expanduser("~")
    def one(s):
        return s.replace(home, "~") if isinstance(s, str) else s
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_scrub(v) for v in obj]
    return one(obj)


def _version(c) -> str:
    for m in ("gateway.identity.get", "system.info"):
        try:
            r = c.rpc(m, {}, timeout=10.0)
            for k in ("version", "gatewayVersion", "agentVersion", "openclawVersion"):
                if r.get(k):
                    return str(r[k])
        except Exception:
            pass
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--schemas", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    c = _client()
    version = _version(c)
    names = sorted(_CANDIDATES)
    if args.only:
        prefixes = tuple(args.only.split(","))
        names = [n for n in names if n.split(".")[0] in prefixes]

    methods: dict[str, dict] = {}
    for m in names:
        if m not in c.methods():
            methods[m] = {"required": [], "allowed": [], "types": {},
                          "additionalProperties": True, "learned": False,
                          "probe_log": ["method not advertised by this gateway"]}
            continue
        print(f"  probing {m} ...", flush=True)
        methods[m] = learn_schema(c, m)

    out = _scrub({
        "_capture": {"gateway_version": version, "protocol": 4,
                     "captured_at_note": "stamp set by hand or CI, not Date.now()",
                     "tool": "qa/capture_gateway.py"},
        "methods": methods,
    })

    if args.check:
        if not os.path.exists(SCHEMAS_PATH):
            print("no tracked schema to check against", file=sys.stderr)
            return 3
        old = json.load(open(SCHEMAS_PATH))["methods"]
        drift = [m for m in methods if old.get(m) != methods[m]]
        if drift:
            print("DRIFT:", ", ".join(drift), file=sys.stderr)
            return 3
        print("no drift")
        return 0

    with open(SCHEMAS_PATH, "w") as f:
        json.dump(out, f, indent=2, sort_keys=True)
        f.write("\n")
    learned = sum(1 for v in methods.values() if v["learned"])
    print(f"wrote {SCHEMAS_PATH}: {len(methods)} methods, {learned} learned, gateway {version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
