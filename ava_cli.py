#!/usr/bin/env python3
"""Ava control CLI — make installing & running Ava easy.

    ava doctor      # check the environment (hardware, dirs, config, services)
    ava setup       # first-run: create AVA_HOME, generate secrets + password, ava.yaml
    ava up          # run the Ava bridge (web app + API)
    ava version

Designed to be friendly on a fresh machine: `ava setup && ava up` gets a new user
from zero to a running Ava with no source edits. See docs/PACKAGING_PLAN.md.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ava_bridge import settings  # noqa: E402

__version__ = "0.1.0"

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[0m"
OK, WARN, BAD = f"{G}✓{X}", f"{Y}●{X}", f"{R}✗{X}"


def _row(mark: str, label: str, detail: str = "") -> None:
    print(f"  {mark} {label:22} {detail}")


def _probe(url: str) -> bool:
    try:
        import requests
        return requests.get(url, timeout=2).status_code < 500
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
def _recommend_tier(avail_gb: float) -> tuple[str, str]:
    """Map available fit-memory (GB) to a concrete model class + example, so a
    user knows what will run on their hardware without guessing."""
    if avail_gb >= 40:
        return "large", "~30B-class models (e.g. the default Nemotron open-model 30B)"
    if avail_gb >= 20:
        return "medium", "~13-14B models, or a quantized 30B"
    if avail_gb >= 12:
        return "small", "~7-8B models (e.g. Llama 3.1 8B, Qwen2.5 7B)"
    if avail_gb >= 6:
        return "tiny", "~3-7B quantized (Q4) models via Ollama"
    return "cloud", "too little local memory — use a hosted API (cloud profile)"


def cmd_doctor(_args) -> int:
    print(f"\n{B}Ava doctor{X}  (AVA_HOME = {settings.AVA_HOME})\n")

    print("Environment")
    _row(OK, "Python", sys.version.split()[0])
    _row(OK if settings.CONFIG_PATH.is_file() else WARN, "Config",
         str(settings.CONFIG_PATH) if settings.CONFIG_PATH.is_file()
         else "no ava.yaml yet — run `ava setup`")
    for d in (settings.data_dir(), settings.logs_dir(), settings.media_dir(),
              settings.secrets_dir()):
        _row(OK if os.path.isdir(d) else WARN, os.path.basename(d) or d, d)

    print("\nHardware")
    try:
        from ava_bridge import hardware
        g = hardware._gpu()
        if g.get("name"):
            _row(OK, "GPU", f"{g['name']} · {g.get('temp','?')}°C · {g.get('power','?')}W")
        else:
            _row(WARN, "GPU", "no NVIDIA GPU detected — use the cpu/cloud profile")
        disk = hardware._disk()
        low = (disk.get("used_pct") or 0) > 90
        _row(BAD if low else OK, "Disk", f"{disk.get('free_gb','?')} GB free")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "Hardware", f"probe failed: {e}")

    # Model-fit recommendation — turn the detected memory into a concrete tier so
    # a forker knows what will actually run on THEIR box (not the author's).
    print("\nModel fit")
    try:
        from ava_bridge import hwinfo
        mem = hwinfo.fit_memory()
        avail = mem.total_gb
        if avail is None:
            _row(WARN, "memory", "couldn't read a fit-memory pool; assume CPU/cloud")
            tier, hint = "cloud", "use a hosted API (cloud) or a tiny quantized model"
        else:
            _row(OK, "fit memory", f"{avail:.0f} GB ({mem.source or 'detected'})")
            tier, hint = _recommend_tier(avail)
        _row(OK, "recommended", f"{tier} — {hint}")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "model fit", f"probe failed: {e}")
        avail = None

    print("\nAgent runtime")
    try:
        from ava_bridge import runtime, config as _cfg
        rt, err = runtime.gate()
        st = runtime.nemoclaw().status()
        _row(OK if st.get("cli") else (BAD if _cfg.AGENT_REQUIRED else WARN),
             "nemoclaw", st.get("cli") or "not installed (`ava agent provision --install`)")
        _row(OK if st.get("sandbox_exists") else (BAD if _cfg.AGENT_REQUIRED else WARN),
             "sandbox", f"{st.get('sandbox')} " +
             ("(exists)" if st.get("sandbox_exists") else "(missing — `nemoclaw onboard`)"))
        if err:
            _row(BAD, "active", f"direct (BLOCKED) — {err}")
        elif rt.name == "direct":
            _row(WARN, "active", "direct (tool-less) — full agent not present")
        else:
            _row(OK, "active", f"{rt.name} — full agent (tools + memory + CoT)")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "agent runtime", f"probe failed: {e}")

    print("\nInference backends")
    backends = (settings.get("inference.backends", {}) or {})
    if not backends:
        _row(WARN, "backends", "none configured in ava.yaml")
    for name, b in backends.items():
        b = b or {}
        url = b.get("base_url", "")
        ok = _probe(url.rstrip("/") + "/models") if url else False
        # Flag a backend whose declared weight won't fit the detected memory.
        weight = ((b.get("fit") or {}).get("weight_gb"))
        note = f"{url}  {'up' if ok else 'unreachable'}"
        icon = OK if ok else WARN
        if weight and avail and float(weight) > float(avail):
            icon = WARN
            note += f"  ⚠ needs ~{weight} GB > {avail:.0f} GB available"
        _row(icon, name, note)

    print("\nBridge")
    port = settings.get_int("server.port", 8096, env="AVA_PORT")
    _row(OK if _probe(f"http://127.0.0.1:{port}/api/health") else WARN,
         "web app", f"http://127.0.0.1:{port}")
    print()
    return 0


# --------------------------------------------------------------------------- #
def cmd_setup(args) -> int:
    print(f"\n{B}Ava setup{X}  (AVA_HOME = {settings.AVA_HOME})\n")
    settings.ensure_dirs()
    _row(OK, "directories", "created under AVA_HOME")

    # signing secret (auto-generated, secure by default)
    settings.secret("session_secret", env="AVA_SECRET", generate=True)
    _row(OK, "session secret", "generated" if not os.environ.get("AVA_SECRET") else "from env")

    # admin password: env -> flag -> existing -> generate
    pw_path = os.path.join(settings.data_dir(), "auth_password")
    if os.environ.get("AVA_PASSWORD"):
        _row(OK, "password", "using AVA_PASSWORD from env")
    elif os.path.isfile(pw_path) and not args.force:
        _row(OK, "password", f"already set ({pw_path})")
    else:
        pw = args.password or __import__("secrets").token_urlsafe(12)
        try:
            with open(pw_path, "w", encoding="utf-8") as f:
                f.write(pw)
            os.chmod(pw_path, 0o600)
        except OSError as e:
            print(f"{BAD} could not write password: {e}")
            return 1
        _row(OK, "password", f"{Y}{pw}{X}   (saved to {pw_path})")

    # ava.yaml starter
    if not settings.CONFIG_PATH.is_file():
        example = os.path.join(settings.CODE_ROOT, "config.example.yaml")
        try:
            if os.path.isfile(example):
                shutil.copyfile(example, settings.CONFIG_PATH)
                _row(OK, "ava.yaml", f"created at {settings.CONFIG_PATH}")
            else:
                _row(WARN, "ava.yaml", "template not found; skipped")
        except OSError as e:
            _row(WARN, "ava.yaml", f"skip: {e}")
    else:
        _row(OK, "ava.yaml", "already exists")

    # model store scaffolding — same hf/ollama/gpusvc layout as the Docker volumes,
    # so a fork's tree matches the author's and `ava models pull` has a home.
    dirs = ensure_model_dirs()
    _row(OK, "model dirs", f"{dirs['root']}/{{hf,ollama,gpusvc}}")
    cp = _write_gpusvc_paths(dirs["gpusvc"])
    _row(OK, "gpusvc paths", cp or "kept existing gpusvc/extra_model_paths.yaml")

    print(f"\n{G}Setup complete.{X} Next:  ava models pull --auto   then   ava doctor   then   ava up\n")
    return 0


# --------------------------------------------------------------------------- #
def cmd_up(args) -> int:
    host = args.host or settings.get("server.host", "0.0.0.0", env="AVA_HOST")
    port = args.port or settings.get_int("server.port", 8096, env="AVA_PORT")
    py = sys.executable
    print(f"{B}Starting Ava{X} on http://{host}:{port}  (Ctrl-C to stop)\n")
    cmd = [py, "-m", "uvicorn", "phone_bridge:app", "--host", str(host),
           "--port", str(port)]
    return subprocess.call(cmd, cwd=settings.CODE_ROOT)


# Version is bumped on each packaged release.
def cmd_version(_args) -> int:
    print(f"ava {__version__}")
    return 0


def cmd_agent(args) -> int:
    """Manage the agent runtime (NemoClaw): status + provisioning."""
    from ava_bridge import runtime, config
    action = args.action or "status"
    if action == "status":
        st = runtime.nemoclaw().status()
        rt, err = runtime.gate()
        print(f"\n{B}Agent runtime{X}\n")
        _row(OK, "configured", config.AGENT_RUNTIME)
        _row(OK if not config.AGENT_REQUIRED else OK, "required", str(config.AGENT_REQUIRED))
        _row(OK if st.get("cli") else WARN, "nemoclaw CLI", st.get("cli") or "not installed")
        _row(OK if st.get("sandbox_exists") else WARN, "sandbox",
             f"{st.get('sandbox')} " + ("(exists)" if st.get("sandbox_exists") else "(missing — run `ava agent provision`)"))
        _row(OK if rt.name != "direct" else (BAD if err else WARN), "active",
             rt.name + (f" — {err}" if err else ""))
        if st.get("health"):
            _row(OK, "health", str(st["health"])[:120])
        print()
        return 0
    if action == "provision":
        print(f"{B}Provisioning the agent runtime (NemoClaw)…{X}")
        res = runtime.nemoclaw().provision(auto_install=args.install)
        for s in res.get("steps", []):
            print(f"  {OK if s['ok'] else BAD} {s['step']}: {s['detail']}")
        print(f"\n{OK if res['ok'] else WARN} {res['detail']}")
        return 0 if res["ok"] else 1
    print(f"{BAD} usage: ava agent status | provision [--install]")
    return 1


_CONNECTOR_TEMPLATE = """\
id: NAME
label: NAME
kind: app                 # core | inference | media | app
enabled: true
service:                  # shown in the dashboard's Service health matrix
  name: NAME
  # probe: "http://127.0.0.1:PORT/health"   # HTTP health check (optional)
  # unit: NAME.service                       # systemd user unit (optional)
# perf:                   # if this app writes an Ava performance.jsonl
#   app: NAME
#   path: "${AVA_HOME}/connectors/NAME/performance.jsonl"
# egress:                 # what Ava's agent tools may reach for this connector
#   routes: ["POST /internal/NAME/do"]   # bridge routes (host.openshell.internal:8096)
#   hosts:  ["127.0.0.1:PORT"]           # or direct endpoints
# actions:                # agent tools this connector exposes
#   - { id: NAME_do, description: "what it does" }
"""


def cmd_connector(args) -> int:
    from ava_bridge import connectors
    if args.action == "list":
        items = connectors.all()
        if not items:
            print("  (no connectors found)")
            return 0
        for m in items:
            svc = m.get("service") or {}
            print(f"  {B}{m['id']:16}{X} {m.get('kind','app'):9} {m.get('label','')}")
            probe = connectors._expand(svc.get("probe")) if svc else None
            if svc:
                print(f"       service: unit={svc.get('unit','-')}  probe={probe or '-'}")
            if m.get("perf"):
                print(f"       perf:    {connectors._expand(m['perf'].get('path'))}")
        return 0
    if args.action == "apps":
        rows = connectors.apps()
        if not rows:
            print("  (no connectors declare a `ui:` block)")
            return 0
        for a in rows:
            extra = f"view={a['view']}" if a["embed"] == "native" else (a.get("url") or "")
            print(f"  {B}{a['id']:16}{X} {a['section']:5} {a['embed']:7} "
                  f"icon={a['icon']:10} {extra}")
        return 0
    if args.action == "new":
        if not args.name:
            print(f"{BAD} usage: ava connector new <name>")
            return 1
        d = os.path.join(settings.home("connectors"), args.name)
        path = os.path.join(d, "connector.yaml")
        if os.path.exists(path):
            print(f"{WARN} already exists: {path}")
            return 1
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_CONNECTOR_TEMPLATE.replace("NAME", args.name))
        print(f"{OK} created {path}")
        print("   edit it, then restart Ava (or `ava up`) to load the connector.")
        return 0
    if args.action == "policies":
        import yaml as _yaml
        ids = [args.name] if args.name else [m["id"] for m in connectors.all()]
        wrote = 0
        for cid in ids:
            pol = connectors.render_egress_policy(cid)
            if not pol:
                continue
            text = _yaml.safe_dump(pol, sort_keys=False)
            if args.write:
                outdir = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
                os.makedirs(outdir, exist_ok=True)
                p = os.path.join(outdir, f"{cid}.yaml")
                with open(p, "w", encoding="utf-8") as f:
                    f.write(text)
                print(f"{OK} wrote {p}")
                wrote += 1
            else:
                print(f"# ---- {cid} ----\n{text}")
        if args.write:
            print(f"\n{wrote} policy file(s) in agent/policies/generated/ — "
                  f"run `cd agent && ./install.sh` to deploy into the sandbox.")
        return 0
    if args.action == "tools":
        ids = [args.name] if args.name else [m["id"] for m in connectors.all()]
        wrote = 0
        for cid in ids:
            m = {x["id"]: x for x in connectors.all()}.get(cid) or {}
            for a in connectors._static_actions(m):
                if not (a.get("id") and a.get("path")):
                    continue  # only generic-proxy actions (with a path) get a tool
                src = connectors.render_tool(cid, a)
                if args.write:
                    outdir = os.path.join(settings.CODE_ROOT, "agent",
                                          "mcp_server_content", "connectors", cid)
                    os.makedirs(outdir, exist_ok=True)
                    p = os.path.join(outdir, f"{cid}_{a['id']}.mjs")
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(src)
                    print(f"{OK} wrote {p}")
                    wrote += 1
                else:
                    print(f"# ---- {cid}_{a['id']}.mjs ----\n{src}")
        if args.write:
            print(f"\n{wrote} tool(s) written — run `cd agent && ./install.sh` to "
                  f"deploy into the sandbox.")
        elif not any((m.get('actions') for m in connectors.all())):
            print("  (no generic-proxy actions to generate tools for)")
        return 0
    return 1


_DEVICE_CONNECTOR_TEMPLATE = """\
# A DEVICE connector: wire your own app (Arduino, Nicla, Portenta, ESP32, a
# smart-home bridge, any sensor) to Ava. YOUR app owns all device I/O; Ava just
# connects to it. See docs/DEVICE_CONNECTORS.md.
id: NAME
label: NAME
kind: app
role: device              # groups it in Ava's Devices view
enabled: true

service:                  # optional health probe -> Ops dashboard
  name: NAME
  # probe: "http://127.0.0.1:PORT/health"

# PULL — Ava reads/commands your devices on demand. Your app exposes an MCP-style
# tool server (GET /tools + POST /call); Ava bridges the whole set live.
actions:
  discover:
    base: "http://127.0.0.1:PORT"
    list: "/tools"
    call: "/call"

# PUSH — let your app hand Ava events when IT decides (motion, thresholds). Get
# the inbound token with `ava device token NAME` and POST to
#   /api/connectors/NAME/events   (Authorization: Bearer <token>)
ingest:
  enabled: true
  channels:               # optional, purely descriptive (nicer Devices view)
    - { name: temperature, unit: "C" }
    - { name: motion, kind: event }

# What Ava's agent may reach for the PULL path (auto-rendered into an egress
# policy by `ava connector policies NAME --write`).
egress:
  routes:
    - "GET /internal/connector/NAME/__tools"
    - "POST /internal/connector/NAME/__call"
"""


def cmd_device(args) -> int:
    """Device connectors: scaffold, inbound token, and recent pushed events."""
    from ava_bridge import connectors, devices, internal

    if args.action == "list":
        rows = connectors.devices()
        if not rows:
            print("  (no device connectors — scaffold one with `ava device new <name>`)")
            return 0
        for d in rows:
            last = devices.last_event_ts(d["id"])
            import time as _t
            when = _t.strftime("%Y-%m-%d %H:%M:%S", _t.localtime(last)) if last else "never"
            caps = ",".join(filter(None, [
                "pull" if d["discover"] else "", "push" if d["ingest"] else ""]))
            print(f"  {B}{d['id']:16}{X} {caps or '-':10} last-event={when}  {d['label']}")
        return 0

    if args.action == "new":
        if not args.name:
            print(f"{BAD} usage: ava device new <name>")
            return 1
        d = os.path.join(settings.home("connectors"), args.name)
        path = os.path.join(d, "connector.yaml")
        if os.path.exists(path):
            print(f"{WARN} already exists: {path}")
            return 1
        os.makedirs(d, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(_DEVICE_CONNECTOR_TEMPLATE.replace("NAME", args.name))
        print(f"{OK} created {path}")
        print(f"   1. edit it (set your app's PORT), then restart Ava (or `ava up`)")
        print(f"   2. get the inbound push token:  ava device token {args.name}")
        print(f"   3. see events arrive:           ava device events {args.name}")
        return 0

    if args.action == "token":
        if not args.name:
            print(f"{BAD} usage: ava device token <id>")
            return 1
        tok = internal.ingest_token(args.name)
        port = settings.get_int("server.port", 8096, env="AVA_PORT")
        url = f"{settings.get('server.public_url', f'http://localhost:{port}')}"
        ep = f"{url.rstrip('/')}/api/connectors/{args.name}/events"
        print(f"{OK} inbound push token for '{args.name}':\n\n  {tok}\n")
        print("   Your app POSTs events to Ava with this bearer token, e.g.:\n")
        print(f"""  curl -s {ep} \\
       -H "Authorization: Bearer {tok}" \\
       -H "Content-Type: application/json" \\
       -d '{{"name":"motion","message":"Front door motion","notify":true}}'\n""")
        print("   (rotating the connector id rotates the token; keep it secret.)")
        return 0

    if args.action == "events":
        rows = devices.recent(args.name or None, limit=args.limit or 50)
        if not rows:
            print("  (no events yet)")
            return 0
        import time as _t
        for e in rows:
            when = _t.strftime("%H:%M:%S", _t.localtime(e.get("ts", 0)))
            val = f" = {e['value']}{(' ' + e['unit']) if e.get('unit') else ''}" if "value" in e else ""
            msg = f"  — {e['message']}" if e.get("message") else ""
            sev = f"  [{e['severity']}]" if e.get("severity") else ""
            print(f"  {when}  {B}{e.get('cid','?')}/{e.get('name','?')}{X}{val}{msg}{sev}")
        return 0
    return 1


# --------------------------------------------------------------------------- #
# Model store — scaffold the weights folders and pull the right model for THIS
# hardware, so a fork reproduces the author's layout with no manual path hunting.
_gpusvc_SUBDIRS = ["checkpoints", "loras", "vae", "guidance net", "upscale_models",
                  "embeddings", "clip", "clip_vision", "unet", "weight_models",
                  "text_encoders"]

_DEFAULT_MODELS = {
    "chat": {"engine": "vllm",
             "id": "nvidia/Nemotron-Open-30B-A3B-Reasoning-FP8", "tier": "large"},
    "fast": {"engine": "ollama", "id": "llama3.1:8b", "tier": "small"},
    "image": {"engine": "gpu-service", "id": "gpu_model_base",
              "dest": "checkpoints",
              "url": "https://huggingface.co/example/gpu-model"
                     "resolve/main/gpu_model_base"},
}


def _models_manifest() -> dict:
    m = settings.get("models", None)
    return m if isinstance(m, dict) and m else dict(_DEFAULT_MODELS)


def _model_dirs() -> dict:
    base = settings.models_dir()
    return {"root": base, "hf": os.path.join(base, "hf"),
            "ollama": os.path.join(base, "ollama"),
            "gpusvc": os.path.join(base, "gpusvc")}


def ensure_model_dirs() -> dict:
    d = _model_dirs()
    for k in ("hf", "ollama", "gpusvc"):
        os.makedirs(d[k], exist_ok=True)
    for sub in _gpusvc_SUBDIRS:
        os.makedirs(os.path.join(d["gpusvc"], sub), exist_ok=True)
    return d


def _write_gpusvc_paths(gpusvc_dir: str) -> str | None:
    """Write gpusvc/extra_model_paths.yaml pointing the GPU service at the model store.
    Never clobbers an existing file (a personal install keeps its own layout)."""
    p = os.path.join(settings.CODE_ROOT, "gpusvc", "extra_model_paths.yaml")
    if os.path.exists(p):
        return None
    os.makedirs(os.path.dirname(p), exist_ok=True)
    lines = ["# Generated by `ava setup` — points the GPU service at $AVA_HOME/models/gpusvc.",
             "ava:", f"    base_path: {gpusvc_dir}"]
    lines += [f"    {sub}: {sub}" for sub in _gpusvc_SUBDIRS]
    with open(p, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return p


def _hf_present(model_id: str, hf_dir: str) -> bool:
    safe = "models--" + model_id.replace("/", "--")
    return any(os.path.isdir(os.path.join(hf_dir, sub, safe)) for sub in ("hub", ""))


def _pull_hf(model_id: str, hf_dir: str) -> int:
    env = {**os.environ, "HF_HOME": hf_dir}
    for exe in ("hf", "huggingface-cli"):
        if shutil.which(exe):
            cmd = [exe, "download", model_id]
            print(f"  $ HF_HOME={hf_dir} {' '.join(cmd)}")
            return subprocess.call(cmd, env=env)
    print(f"  {WARN} huggingface CLI not found — pip install 'huggingface_hub[cli]'")
    return 1


def _ollama_env(ollama_dir: str) -> dict:
    return {**os.environ, "OLLAMA_MODELS": ollama_dir}


def _ollama_present(tag: str, ollama_dir: str) -> bool:
    if not shutil.which("ollama"):
        return False
    try:
        out = subprocess.run(["ollama", "list"], env=_ollama_env(ollama_dir),
                             capture_output=True, text=True, timeout=10)
        return tag.split(":")[0] in out.stdout
    except Exception:  # noqa: BLE001
        return False


def _pull_ollama(tag: str, ollama_dir: str) -> int:
    if not shutil.which("ollama"):
        print(f"  {WARN} ollama not installed — https://ollama.com/download")
        return 1
    print(f"  $ OLLAMA_MODELS={ollama_dir} ollama pull {tag}")
    return subprocess.call(["ollama", "pull", tag], env=_ollama_env(ollama_dir))


def _gpusvc_target(spec: dict, gpusvc_dir: str) -> str:
    return os.path.join(gpusvc_dir, spec.get("dest", "checkpoints"),
                        os.path.basename(spec["id"]))


def _gpusvc_present(spec: dict, gpusvc_dir: str) -> bool:
    return os.path.isfile(_gpusvc_target(spec, gpusvc_dir))


def _pull_gpusvc(spec: dict, gpusvc_dir: str) -> int:
    target = _gpusvc_target(spec, gpusvc_dir)
    url = spec.get("url")
    if not url:
        print(f"  {WARN} no url for {spec['id']} — place the file at {target} by hand")
        return 1
    os.makedirs(os.path.dirname(target), exist_ok=True)
    try:
        import requests
    except Exception:  # noqa: BLE001
        print(f"  {BAD} requests not available; download {url} to {target} by hand")
        return 1
    tmp = target + ".part"
    print(f"  \u2193 {url}")
    try:
        with requests.get(url, stream=True, timeout=60) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length") or 0)
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r    {done * 100 // total:3d}%  "
                              f"{done >> 20}/{total >> 20} MiB", end="", flush=True)
            print()
        os.replace(tmp, target)
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"  {BAD} download failed: {e}")
        try:
            os.remove(tmp)
        except OSError:
            pass
        return 1


def _model_present(spec: dict, dirs: dict) -> bool:
    eng = spec.get("engine")
    if eng == "vllm":
        return _hf_present(spec["id"], dirs["hf"])
    if eng == "ollama":
        return _ollama_present(spec["id"], dirs["ollama"])
    if eng == "gpu-service":
        return _gpusvc_present(spec, dirs["gpusvc"])
    return False


def _pull_one(role: str, spec: dict, dirs: dict) -> int:
    eng = spec.get("engine")
    print(f"{B}{role}{X}  {spec.get('id')}  ({eng})")
    if _model_present(spec, dirs):
        print(f"  {OK} already present")
        return 0
    if eng == "vllm":
        return _pull_hf(spec["id"], dirs["hf"])
    if eng == "ollama":
        return _pull_ollama(spec["id"], dirs["ollama"])
    if eng == "gpu-service":
        return _pull_gpusvc(spec, dirs["gpusvc"])
    print(f"  {WARN} unknown engine '{eng}' — skipped")
    return 1


def _detected_tier() -> tuple[str, float | None]:
    try:
        from ava_bridge import hwinfo
        avail = hwinfo.fit_memory().total_gb
    except Exception:  # noqa: BLE001
        avail = None
    if avail is None:
        return "cloud", None
    return _recommend_tier(avail)[0], avail


def cmd_models(args) -> int:
    manifest = _models_manifest()
    dirs = _model_dirs()
    if args.action == "list":
        tier, avail = _detected_tier()
        print(f"\n{B}Model store{X}  ({dirs['root']})   detected tier: {tier}"
              + (f" \u00b7 {avail:.0f} GB" if avail else "") + "\n")
        for role, spec in manifest.items():
            present = _model_present(spec, dirs)
            _row(OK if present else WARN, role,
                 f"{spec.get('id')}  [{spec.get('engine')}, tier={spec.get('tier', '?')}]  "
                 + ("present" if present else "not downloaded"))
        print()
        return 0
    if args.action == "verify":
        ok = True
        for role, spec in manifest.items():
            present = _model_present(spec, dirs)
            _row(OK if present else WARN, role,
                 "present" if present else "missing (`ava models pull`)")
            ok = ok and present
        return 0 if ok else 1
    if args.action == "pull":
        ensure_model_dirs()
        if args.auto:
            tier, _avail = _detected_tier()
            role = {"large": "chat", "medium": "chat", "small": "fast",
                    "tiny": "fast"}.get(tier)
            if not role or role not in manifest:
                print(f"{WARN} detected tier '{tier}' — no local model fits; use a cloud "
                      f"provider (set inference.backends + AVA_INFERENCE_KEY).")
                return 0
            print(f"{B}--auto{X}: detected tier '{tier}' \u2192 pulling '{role}'\n")
            return _pull_one(role, manifest[role], dirs)
        roles = [args.name] if args.name else list(manifest)
        rc = 0
        for role in roles:
            if role not in manifest:
                print(f"{WARN} no such role '{role}' (have: {', '.join(manifest)})")
                rc = 1
                continue
            rc = _pull_one(role, manifest[role], dirs) or rc
        return rc
    return 1


def main() -> int:
    p = argparse.ArgumentParser(prog="ava", description="Ava control CLI")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sp = sub.add_parser("setup", help="first-run setup (dirs, secrets, password, ava.yaml)")
    sp.add_argument("--password", help="set the admin password (else one is generated)")
    sp.add_argument("--force", action="store_true", help="overwrite an existing password")
    sp.set_defaults(func=cmd_setup)
    up = sub.add_parser("up", help="run the Ava bridge (web app + API)")
    up.add_argument("--host")
    up.add_argument("--port", type=int)
    up.set_defaults(func=cmd_up)
    sub.add_parser("version", help="print version").set_defaults(func=cmd_version)
    ap = sub.add_parser("agent", help="agent runtime (NemoClaw): status / provision")
    ap.add_argument("action", nargs="?", choices=["status", "provision"], default="status")
    ap.add_argument("--install", action="store_true",
                    help="auto `npm install -g nemoclaw` if the CLI is missing")
    ap.set_defaults(func=cmd_agent)
    cp = sub.add_parser("connector", help="list / scaffold / generate policies+tools for connectors")
    cp.add_argument("action", choices=["list", "apps", "new", "policies", "tools"])
    cp.add_argument("name", nargs="?", help="connector name (for new / policies / tools)")
    cp.add_argument("--write", action="store_true", help="write generated files (policies -> agent/policies/generated, tools -> agent/mcp_server_content/connectors)")
    cp.set_defaults(func=cmd_connector)
    dp = sub.add_parser("device", help="wire your own device/sensor app to Ava: scaffold / token / events")
    dp.add_argument("action", choices=["list", "new", "token", "events"])
    dp.add_argument("name", nargs="?", help="device connector id (for new / token / events)")
    dp.add_argument("--limit", type=int, default=0, help="max events to show (events)")
    dp.set_defaults(func=cmd_device)
    mp = sub.add_parser("models", help="model store: list / pull / verify weights")
    mp.add_argument("action", choices=["list", "pull", "verify"])
    mp.add_argument("name", nargs="?", help="role to pull (chat/fast/image); default all")
    mp.add_argument("--auto", action="store_true",
                    help="pull the chat/fast model that fits the detected memory tier")
    mp.set_defaults(func=cmd_models)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
