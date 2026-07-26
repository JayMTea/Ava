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
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ava_bridge import settings  # noqa: E402
from ava_bridge.version import __version__, revision  # noqa: E402

G, Y, R, B, X = "\033[32m", "\033[33m", "\033[31m", "\033[34m", "\033[0m"
OK, WARN, BAD = f"{G}+{X}", f"{Y}●{X}", f"{R}x{X}"


def _row(mark: str, label: str, detail: str = "") -> None:
    print(f"  {mark} {label:22} {detail}")


def _probe(url: str) -> bool:
    try:
        import requests
        return requests.get(url, timeout=2).status_code < 500
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Tier recommendation lives in model_fit so doctor / models pull / the setup
# wizard share one source of truth.
from ava_bridge.model_fit import recommend_tier as _recommend_tier  # noqa: E402


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

    # Allocation — which declared models are actually resident, and whether the
    # box could bring each one up right now. This is where "the unit is active but
    # the model never loaded" becomes visible instead of silent.
    print("\nAllocation")
    try:
        from ava_bridge import alloc
        rep = alloc.report()
        pool = rep["pool"]
        lz = rep.get("leases") or {}
        # Advisory vs enforcing is the single most important thing to state plainly:
        # an operator reading this must never be unsure whether Ava is allowed to act.
        _row(OK, "mode", "ENFORCING — may release declared models to make room"
             if rep.get("actuating") else
             "advisory — decisions recorded to logs/alloc.jsonl, nothing is actuated")
        led = lz.get("ledger") or {}
        if led.get("writable") is False:
            _row(BAD, "ledger", f"not writable: {led.get('dir')} — leases cannot "
                                "coordinate across processes")
        elif lz.get("lease_count"):
            _row(OK, "leases", f"{lz['lease_count']} held · {len(lz.get('owed') or [])} "
                               f"awaiting restore · ledger on {led.get('fstype')}")
        for od in (lz.get("overdue") or []):
            _row(WARN, "overdue", f"lease {od['lease_id']} (pid {od['pid']}) has held "
                                  f"{', '.join(od['models'] or [])} for {od['age_s']}s")
        if rep["gating"] == "disabled":
            _row(WARN, "pool", "memory unreadable — allocation is not gating anything")
        else:
            bits = [f"{pool['free_gib']:.0f} GB free of {pool['total_gib']:.0f}",
                    f"({pool['source']})"]
            if pool.get("baseline_gib") is not None:
                bits.append(f"· baseline {pool['baseline_gib']:.0f}")
            if pool.get("unknown_gib"):
                bits.append(f"· {pool['unknown_gib']:.0f} held by undeclared processes")
            _row(OK, "pool", " ".join(bits))
        if not rep["models"]:
            _row(OK, "declared", "no models declared — nothing is governed "
                                 "(add `alloc.models` in ava.yaml to opt in)")
        for m in rep["models"]:
            # A declared model that is resident but NOT ready is the dangerous
            # state: its port is open, so nothing else notices it is serving
            # nothing. Call that out ahead of anything else.
            if m["resident"] and m["ready"] is False:
                mark, detail = BAD, f"resident but NOT ready — {m['detail']}"
            elif not m["cold_load_ok"]:
                mark, detail = WARN, f"would not fit now — {m['cold_load_reason']}"
            elif m["resident"] is None:
                mark, detail = OK, f"{m['driver']} · {m['detail']}"
            else:
                gib = m["resident_gib"]
                shown = f"{gib:.1f} GB" if gib is not None else "size unknown"
                state = "resident" if m["resident"] else "not resident"
                mark, detail = OK, (f"{m['driver']} · {state} · {shown}"
                                    f"{'' if m['measured'] else ' (declared, unmeasured)'}")
            _row(mark, m["id"], detail)
            for prob in m["problems"]:
                _row(WARN, "", prob)
    except Exception as e:  # noqa: BLE001 — reporting must never fail doctor
        _row(WARN, "allocation", f"probe failed: {e}")

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
        if st.get("sandbox_model"):
            # The model the agent actually thinks with — decided by `nemoclaw
            # onboard`, not ava.yaml, so doctor must not stay blind to it.
            _row(OK, "brain", f"{st['sandbox_model']} (sandbox — via `nemoclaw onboard`)")
        if err:
            _row(BAD, "active", f"direct (BLOCKED) — {err}")
        elif rt.name == "direct":
            _row(WARN, "active", "direct (tool-less) — full agent not present")
        else:
            _row(OK, "active", f"{rt.name} — full agent (tools + memory + CoT)")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "agent runtime", f"probe failed: {e}")

    print("\nConnectors")
    try:
        from ava_bridge import connectors as _conn
        _conn.load(force=True)          # populates load_errors()
        cat = _conn.catalog()
        on = len([m for m in cat if m.get("enabled", True)])
        _row(OK, "loaded", f"{on} enabled, {len(cat) - on} disabled")
        errs = _conn.load_errors()
        if errs:
            for e in errs:
                _row(BAD, "manifest", f"{e['id']}: {e['error']}")
        else:
            _row(OK, "manifests", "all parsed cleanly")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "connectors", f"probe failed: {e}")

    print("\nInference backends")
    backends = (settings.get("inference.backends", {}) or {})
    if not backends:
        # "None configured" is only a problem when nothing else serves. With an
        # onboarded agent sandbox, chat thinks with the sandbox model and the
        # yaml block is an optional fallback — say so instead of warning.
        sbx_model = None
        try:
            from ava_bridge import runtime as _rt
            sbx_model = (_rt.nemoclaw().sandbox_info() or {}).get("model")
        except Exception:  # noqa: BLE001
            pass
        if sbx_model:
            _row(OK, "backends",
                 f"none in ava.yaml — chat thinks with the agent sandbox model "
                 f"({sbx_model}); a yaml backend is an optional fallback")
        else:
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
            note += f"  ! needs ~{weight} GB > {avail:.0f} GB available"
        _row(icon, name, note)

    # The route chat ACTUALLY uses (fixes the old doctor/reality mismatch where
    # backends probed green on :8002 while chat errored against :8010).
    print("\nInference route (what chat actually uses)")
    try:
        from ava_bridge import config as _bcfg, router_host
        rst = router_host.router_status()
        default_chat = f"http://127.0.0.1:{_bcfg.ROUTER_PORT}/v1/chat/completions"
        if _bcfg.ROUTER_CHAT_URL != default_chat:
            up = _probe(_bcfg.ROUTER_CHAT_URL.rsplit("/chat/completions", 1)[0]
                        + "/models")
            _row(OK if up else WARN, "chat url",
                 f"{_bcfg.ROUTER_CHAT_URL}  (router bypass — no failover/perf log)")
        elif rst["alive"]:
            _row(OK, "router", f":{rst['port']} up")
        elif rst["embedded_setting"] in ("false", "0", "no", "off"):
            _row(BAD, "router", f":{rst['port']} down and inference.router.embedded "
                 "is false — chat will error")
        else:
            _row(OK, "router", f":{rst['port']} not running — `ava up` starts it "
                 "embedded")
        chat_role = settings.role_backend("chat")
        if chat_role:
            declared = chat_role in (settings.get("inference.backends", {}) or {})
            _row(OK if declared else BAD, "chat role",
                 chat_role + ("" if declared else "  ! not a declared backend"))
    except Exception as e:  # noqa: BLE001
        _row(WARN, "inference route", f"probe failed: {e}")

    print("\nIntent routing")
    try:
        from ava_bridge import eval_router, turn_router
        _row(OK, "mode", f"{turn_router.mode()} · router {turn_router.fingerprint()}")
        cases = eval_router.load_cases()
        if not cases:
            _row(WARN, "eval set", "none yet — build one with `ava eval intent mine`")
        else:
            need, why = eval_router.needs_rerun()
            _row(WARN if need else OK, "eval set", f"{len(cases)} cases — {why}")
    except Exception as e:  # noqa: BLE001
        _row(WARN, "intent routing", f"probe failed: {e}")

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

    # router token (guards /which /route /fit; also /v1/* when LAN-exposed)
    settings.secret("router_token", env="AVA_ROUTER_TOKEN", generate=True)
    _row(OK, "router token", "generated" if not os.environ.get("AVA_ROUTER_TOKEN") else "from env")

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
    created = False
    if not settings.CONFIG_PATH.is_file():
        example = os.path.join(settings.CODE_ROOT, "config.example.yaml")
        try:
            if os.path.isfile(example):
                shutil.copyfile(example, settings.CONFIG_PATH)
                created = True
                _row(OK, "ava.yaml", f"created at {settings.CONFIG_PATH}")
            else:
                _row(WARN, "ava.yaml", "template not found; skipped")
        except OSError as e:
            _row(WARN, "ava.yaml", f"skip: {e}")
    else:
        _row(OK, "ava.yaml", "already exists")

    # The shipped default backend is vLLM (NVIDIA-only). On a box that can't serve
    # it (Apple Silicon, CPU-only), rewrite the fresh inference block to point at a
    # servable local engine — the SAME model `ava models pull --auto` will fetch —
    # so a Mac user isn't left pointing at a dead vLLM endpoint. Best-effort.
    if created and _platform_label() not in ("linux-nvidia", "windows-nvidia"):
        try:
            tier, _avail = _detected_tier()
            _role, spec, _note = _resolve_auto(tier, _models_manifest())
        except Exception:  # noqa: BLE001
            spec = None
        if spec and spec.get("engine") in _LOCAL_CHAT_ENGINES:
            if _seed_inference_backend(spec):
                _row(OK, "inference",
                     f"seeded {spec['engine']} backend ({spec['id']}) for "
                     f"{_platform_label()} — the vLLM default won't run here")
        elif spec is None:
            _row(WARN, "inference",
                 f"no local engine fits {_platform_label()}; configure a cloud "
                 "backend (see config.example.yaml) or install Ollama/MLX")

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


def cmd_verify(_args) -> int:
    """End-to-end claim check: exercise the golden path and confirm each
    advertised capability is actually wired. Prints + / ● / x per check and
    exits non-zero if any HARD check (x) fails. ● (warn) never fails the run —
    it flags an optional capability that isn't set up, not a broken claim."""
    import yaml as _yaml
    from ava_bridge import connectors, config, access_policy, learning
    import ava_learning_digest as _dig

    print(f"\n{B}Ava verify{X}  (AVA_HOME = {settings.AVA_HOME})\n")
    fails = 0

    # 1. Connector SDK — one manifest generates tools + egress policy, in lockstep
    print("Connector SDK  (manifest → tools + egress policy)")
    pol_dir = os.path.join(settings.CODE_ROOT, "agent", "policies", "generated")
    tool_root = os.path.join(settings.CODE_ROOT, "agent", "mcp_server_content", "connectors")
    pol_drift, tool_drift, lockstep_gap = [], [], []
    if os.path.isdir(pol_dir):
        for name in sorted(os.listdir(pol_dir)):
            if not name.endswith(".yaml"):
                continue
            pol = connectors.render_egress_policy(name[:-5])
            with open(os.path.join(pol_dir, name), encoding="utf-8") as f:
                committed = f.read()
            if pol is None or _yaml.safe_dump(pol, sort_keys=False) != committed:
                pol_drift.append(name)
    for cid, m in {x["id"]: x for x in connectors.all()}.items():
        pol = connectors.render_egress_policy(cid) or {}
        allowed = {(r.get("allow", {}).get("method"), r.get("allow", {}).get("path"))
                   for np in pol.get("network_policies", {}).values()
                   for ep in np.get("endpoints", []) for r in ep.get("rules", [])}
        meta = connectors.tool_files(cid)
        for t in meta:
            tp = os.path.join(tool_root, cid, t["name"])
            if not os.path.exists(tp):
                tool_drift.append(f"{t['name'][:-4]} (missing)")
            else:
                with open(tp, encoding="utf-8") as f:
                    if t["source"] != f.read():
                        tool_drift.append(t["name"][:-4])
        # Lockstep: every route a generated tool calls must be in the policy —
        # __tools/__call for meta/dynamic connectors, per-action routes otherwise.
        if connectors.meta_static(m) or connectors._discover_spec(m) or connectors._mcp_spec(m):
            if meta and (("GET", f"/internal/connector/{cid}/__tools") not in allowed
                         or ("POST", f"/internal/connector/{cid}/__call") not in allowed):
                lockstep_gap.append(f"{cid} (__tools/__call)")
        else:
            for a in connectors._static_actions(m):
                if not (a.get("id") and a.get("path")):
                    continue
                route = f"/internal/connector/{cid}/{a['id']}"
                if ("GET", route) not in allowed or ("POST", route) not in allowed:
                    lockstep_gap.append(f"{cid}_{a['id']}")
    _row(BAD if pol_drift else OK, "egress policies",
         f"{len(pol_drift)} stale — `ava connector policies --write`" if pol_drift
         else "match committed (regen clean)")
    _row(BAD if tool_drift else OK, "agent tools",
         f"{len(tool_drift)} stale — `ava connector tools --write`" if tool_drift
         else "match committed (regen clean)")
    _row(BAD if lockstep_gap else OK, "tool <-> policy",
         f"{len(lockstep_gap)} route(s) not allow-listed" if lockstep_gap
         else "every action route allow-listed")
    fails += bool(pol_drift) + bool(tool_drift) + bool(lockstep_gap)

    # 2. Self-editing governance
    print("\nSelf-editing governance")
    _row(OK if config.CODE_APPROVAL in ("all", "policy", "none") else BAD,
         "code.approval", config.CODE_APPROVAL)
    denied_ok = (access_policy.classify(".env") == "denied"
                 and access_policy.classify("models/voiceprint.npy") == "denied")
    _row(OK if denied_ok else BAD, "secrets hard-denied",
         ".env / models/** never writable" if denied_ok else "A SECRET PATH IS NOT DENIED")
    gated_ok = access_policy.classify("ava_bridge/auth.py") == "approval"
    _row(OK if gated_ok else WARN, "sensitive gated",
         "auth/config -> approval" if gated_ok else "auth.py not gated?")
    _row(OK if config.ANTHROPIC_API_KEY else WARN, "code model key",
         "ANTHROPIC_API_KEY present" if config.ANTHROPIC_API_KEY
         else "absent — self-edit disabled until set")
    fails += (not denied_ok)

    # 3. Learning (self-analysis)
    print("\nLearning (self-analysis)")
    _row(OK if config.LEARNING_ENABLED else WARN, "features.learning",
         f"on · every {config.LEARNING_INTERVAL_H}h" if config.LEARNING_ENABLED else "off")
    has_sched = callable(getattr(learning, "start_scheduler", None)) and \
        callable(getattr(learning, "run_all_cycles", None))
    _row(OK if has_sched else BAD, "cycle wiring",
         "scheduler + run_all_cycles present" if has_sched else "MISSING")
    html, _ = _dig.format_digest_html(
        {"cycles": [{"id": "c", "proposals": []}],
         "inline_fixes": [{"fix_applied": "raised timeout", "critical": False}]}, {})
    digest_ok = "raised timeout" in html and "<li>?" not in html
    _row(OK if digest_ok else BAD, "digest content",
         "renders real data" if digest_ok else "placeholder '?' bug")
    fails += (not has_sched) + (not digest_ok)

    # 3b. Long-term memory (governed recall)
    print("\nMemory (long-term recall)")
    if not config.MEMORY_ENABLED:
        _row(WARN, "features.memory", "off — no recall, no distillation")
    else:
        from ava_bridge import memory_store
        store_ok, detail = memory_store.self_check()
        _row(OK if store_ok else BAD, "store (SQLite FTS5)",
             detail if store_ok else f"BROKEN: {detail}")
        distill_ok = callable(getattr(learning, "memory_distiller", None) and
                              getattr(learning.memory_distiller, "run_cycle", None))
        _row(OK if distill_ok else BAD, "distiller wiring",
             "memory_distiller in run_all_cycles" if distill_ok else "MISSING")
        fails += (not store_ok) + (not distill_ok)

    # 4. Voice / biometric (optional capability)
    print("\nVoice / biometric")
    from ava_bridge import features
    voice_on = features.enabled("voice")
    enrolled = any(os.path.exists(os.path.join(base, "models", "voiceprint.npy"))
                   for base in (settings.AVA_HOME, settings.CODE_ROOT))
    if not voice_on:
        _row(WARN, "features.voice", "off (enable + enroll for voiceprint gate)")
    else:
        _row(OK if enrolled else WARN, "voiceprint",
             "enrolled" if enrolled else "voice on but no voiceprint — run enrollment")
        # The flag alone can't prove voice works: check the operative deps too
        # (same probe the Hub voice panel runs), so this report and the Hub
        # can't disagree about a voice-on-but-broken install.
        try:
            from ava_bridge import voice_enroll
            d = voice_enroll.deps()
            deps_ok = bool(d.get("voice") and d.get("ffmpeg"))
            _row(OK if deps_ok else WARN, "voice deps",
                 "installed" if deps_ok else (d.get("error") or "missing"))
        except Exception as e:  # noqa: BLE001
            _row(WARN, "voice deps", f"probe failed: {e}")

    # 5. Inference + health (best-effort; needs services up)
    print("\nInference / health  (best-effort — needs `ava up`)")
    port = settings.get_int("server.port", 8096, env="AVA_PORT")
    _row(OK if _probe(config.ROUTER_CHAT_URL.replace("/v1/chat/completions", "/healthz")) else WARN,
         "router", "up" if _probe(config.ROUTER_CHAT_URL.replace("/v1/chat/completions", "/healthz"))
         else "not reachable (start with `ava up`)")
    _row(OK if _probe(f"http://127.0.0.1:{port}/api/health") else WARN,
         "bridge health", "serving" if _probe(f"http://127.0.0.1:{port}/api/health")
         else "not serving (start with `ava up`)")

    print()
    if fails:
        print(f"{BAD} verify: {fails} hard check(s) failed — fix the x rows above.\n")
        return 1
    print(f"{OK} verify: all hard checks passed. (● rows are optional capabilities.)\n")
    return 0


def cmd_version(_args) -> int:
    rev = revision()
    print(f"ava {__version__}" + (f" ({rev})" if rev else ""))
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
            # tool_files decides the shape: find/call meta tools for dynamic or
            # large static connectors, else one tool per generic-proxy action.
            for t in connectors.tool_files(cid):
                if args.write:
                    outdir = os.path.join(settings.CODE_ROOT, "agent",
                                          "mcp_server_content", "connectors", cid)
                    os.makedirs(outdir, exist_ok=True)
                    p = os.path.join(outdir, t["name"])
                    with open(p, "w", encoding="utf-8") as f:
                        f.write(t["source"])
                    print(f"{OK} wrote {p}")
                    wrote += 1
                else:
                    print(f"# ---- {t['name']} ----\n{t['source']}")
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


def cmd_app(args) -> int:
    """Developer-side scaffold: generate the ava-tools/1 facade INSIDE your app
    repo (surface file + connector manifest + quickstart). The Shopify-CLI
    counterpart to `ava connector new` (which scaffolds Ava's half)."""
    from ava_bridge import scaffold

    if args.action != "new" or not args.name:
        print(f"{BAD} usage: ava app new <id> [--framework fastapi|flask|express|stdlib]"
              f" [--port 9000] [--dir PATH] [--ui]")
        return 1
    try:
        written = scaffold.create(args.name, framework=args.framework,
                                  port=args.port, out_dir=args.dir, ui=args.ui)
    except (ValueError, FileExistsError) as e:
        print(f"{BAD} {e}")
        return 1
    for p in written:
        print(f"{OK} wrote {p}")
    print(f"\nNext: {os.path.join(args.dir, 'ava', 'README-AVA.md')} — wire it up, "
          f"add your tools, then connect http://127.0.0.1:{args.port} in Ava's Hub.")
    return 0


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
        print("   1. edit it (set your app's PORT), then restart Ava (or `ava up`)")
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

# Chat models for boxes that can't serve the vLLM default (Apple Silicon, CPU-only):
# vLLM needs CUDA/ROCm, so `pull --auto` must never fetch it there. Keyed by the
# memory tier, mirrors the Apple-Silicon example in config.example.yaml (Ollama's
# OpenAI-compatible API on :11434 reads the same unified-memory pool).
_OLLAMA_CHAT = {
    "large": {"engine": "ollama", "id": "llama3.1:70b", "tier": "large"},
    "medium": {"engine": "ollama", "id": "llama3.1:8b", "tier": "small"},
}


def _models_manifest() -> dict:
    """Default roles overlaid with the user's `models:` block. A partial
    override (e.g. declaring only `image`) must NOT delete the default chat/
    fast roles — that silently killed `pull --auto`. Set a role to null/false
    in ava.yaml to genuinely remove it."""
    m = settings.get("models", None)
    if not isinstance(m, dict) or not m:
        return dict(_DEFAULT_MODELS)
    merged = {**_DEFAULT_MODELS, **m}
    return {k: v for k, v in merged.items() if isinstance(v, dict)}


def _model_dirs() -> dict:
    base = settings.models_dir()
    return {"root": base, "hf": os.path.join(base, "hf"),
            "ollama": os.path.join(base, "ollama"),
            "gpusvc": os.path.join(base, "gpusvc"),
            "gguf": os.path.join(base, "gguf")}


def ensure_model_dirs() -> dict:
    d = _model_dirs()
    for k in ("hf", "ollama", "gpusvc", "gguf"):
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


def _gguf_target(spec: dict, gguf_dir: str) -> str:
    name = os.path.basename(spec.get("id") or spec.get("url") or "model.gguf")
    return os.path.join(gguf_dir, name)


def _gguf_present(spec: dict, gguf_dir: str) -> bool:
    return os.path.isfile(_gguf_target(spec, gguf_dir))


def _pull_gguf(spec: dict, gguf_dir: str) -> int:
    """Direct-URL GGUF download for llama.cpp — same streaming path as gpusvc."""
    return _download_url(spec.get("url"), _gguf_target(spec, gguf_dir),
                         spec.get("id", "model"))


def _pull_gpusvc(spec: dict, gpusvc_dir: str) -> int:
    return _download_url(spec.get("url"), _gpusvc_target(spec, gpusvc_dir),
                         spec.get("id", "model"))


def _download_url(url: str | None, target: str, label: str) -> int:
    if not url:
        print(f"  {WARN} no url for {label} — place the file at {target} by hand")
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
            # Disk-space precheck: refuse a multi-GB pull that can't fit (+5%
            # headroom) rather than fail mid-write with a cryptic errno.
            if total:
                free = shutil.disk_usage(os.path.dirname(target)).free
                need = int(total * 1.05)
                if free < need:
                    print(f"  {BAD} not enough disk: need ~{need >> 20} MiB, "
                          f"have {free >> 20} MiB free at {os.path.dirname(target)}")
                    return 1
                print(f"    ({total >> 20} MiB, {free >> 30} GiB free)")
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total:
                        print(f"\r    {done * 100 // total:3d}%  "
                              f"{done >> 20}/{total >> 20} MiB", end="", flush=True)
            print()
        # Integrity: a truncated stream (server closed early without raising)
        # must NOT be promoted to the final file and reported "present".
        if total and done != total:
            print(f"  {BAD} truncated download: got {done} of {total} bytes")
            os.remove(tmp)
            return 1
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
    if eng in ("llamacpp", "gguf"):
        return _gguf_present(spec, dirs["gguf"])
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
    if eng in ("llamacpp", "gguf"):
        return _pull_gguf(spec, dirs["gguf"])
    print(f"  {WARN} unknown engine '{eng}' — skipped")
    return 1


def _backend_stanza(role: str, spec: dict, dirs: dict) -> str:
    """A copy-pasteable ava.yaml `inference` stanza for a just-pulled model, so
    pull -> configure is one flow instead of a docs hunt."""
    eng = spec.get("engine", "openai")
    base = {"vllm": "http://127.0.0.1:8002/v1",
            "ollama": "http://127.0.0.1:11434/v1",
            "llamacpp": "http://127.0.0.1:8080/v1",
            "gguf": "http://127.0.0.1:8080/v1"}.get(eng, "http://127.0.0.1:8002/v1")
    bid = f"local-{role}"
    lines = [
        "inference:",
        f"  primary: {bid}",
        "  backends:",
        f"    {bid}:",
        f"      engine: {'llamacpp' if eng == 'gguf' else eng}",
        f"      base_url: {base}",
        f"      model: {spec.get('id')}",
    ]
    if eng in ("llamacpp", "gguf"):
        lines.append(f"      # serve it:  llama-server -m {_gguf_target(spec, dirs['gguf'])} "
                     "--port 8080 --jinja")
        lines.append("      tools: none   # set to native only with a tool-call chat template")
    return "\n".join(lines)


def _detected_tier() -> tuple[str, float | None]:
    try:
        from ava_bridge import hwinfo
        avail = hwinfo.fit_memory().total_gb
    except Exception:  # noqa: BLE001
        avail = None
    if avail is None:
        return "cloud", None
    return _recommend_tier(avail)[0], avail


def _platform_label() -> str:
    """Coarse platform id for user-facing messages (never raises)."""
    try:
        from ava_bridge import hwinfo
        return hwinfo.platform_id()
    except Exception:  # noqa: BLE001
        return "this platform"


# Local chat engines Ava can seed a backend for on a non-CUDA box (all serve an
# OpenAI-compatible API; vLLM is deliberately absent — it needs a CUDA/ROCm GPU).
_LOCAL_CHAT_ENGINES = {"ollama", "llamacpp", "gguf", "mlx", "mlx-lm",
                       "lmstudio", "lm-studio"}


def _seed_inference_backend(spec: dict) -> bool:
    """Rewrite ava.yaml's `inference` block to a single servable local backend
    matching `spec` (engine + model), replacing the vLLM default. Best-effort:
    returns True only if it rewrote the file. Called only on non-CUDA platforms
    from `ava setup`, so a Mac never boots pointing at a dead vLLM endpoint."""
    try:
        import yaml as _yaml
    except Exception:  # noqa: BLE001 — PyYAML absent; leave the example in place
        return False
    if not settings.CONFIG_PATH.is_file():
        return False
    try:
        cfg = _yaml.safe_load(settings.CONFIG_PATH.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return False
    eng = str(spec.get("engine", "ollama")).strip().lower()
    base = {"llamacpp": "http://127.0.0.1:8080/v1",
            "gguf": "http://127.0.0.1:8080/v1"}.get(eng, "http://127.0.0.1:11434/v1")
    bid = "local-ollama" if eng == "ollama" else f"local-{eng}"
    cfg["inference"] = {
        "primary": bid,
        "backends": {bid: {
            "engine": "llamacpp" if eng == "gguf" else eng,
            "base_url": base,
            "model": spec.get("id"),
            "fit": {"tier": spec.get("tier", "large"),
                    "workloads": ["chat", "reasoning", "code"]},
        }},
    }
    try:
        settings.CONFIG_PATH.write_text(
            _yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True), encoding="utf-8")
        return True
    except Exception:  # noqa: BLE001
        return False


def _engine_servable_here(engine: str | None) -> bool:
    """Can THIS box actually run a local engine of this type?

    vLLM needs a CUDA/ROCm GPU, so it can't serve on Apple Silicon or a CPU-only
    box — pulling ~35 GB of FP8 weights there is a trap. Every other local engine
    (Ollama, llama.cpp, MLX, LM Studio) and all cloud engines run anywhere. When
    the platform is unknown we don't block (return True) — degrade, never brick.
    """
    eng = (engine or "").strip().lower()
    if eng == "vllm":
        return _platform_label() in ("linux-nvidia", "windows-nvidia")
    return True


def _resolve_auto(tier: str, manifest: dict) -> tuple:
    """Pick (role, spec, note) for `ava models pull --auto`, platform-aware.

    Guarantees the returned spec's engine is servable on THIS box, so a high-RAM
    Mac (tier 'large') is never steered into the vLLM-only default it can't run.
    Substitutes a same-tier Ollama chat model, or downshifts to the servable
    'fast' role; `note` explains any substitution. Returns (None, None, note) when
    nothing local is servable (caller should point at a cloud provider)."""
    role = {"large": "chat", "medium": "chat",
            "small": "fast", "tiny": "fast"}.get(tier)
    if not role or role not in manifest:
        return None, None, None
    spec = manifest[role]
    if _engine_servable_here(spec.get("engine")):
        return role, spec, None
    plat = _platform_label()
    blocked = (f"the default '{role}' model ({spec.get('engine')}: "
               f"{spec.get('id')}) can't be served on {plat}")
    sub = _OLLAMA_CHAT.get(tier)
    if sub and _engine_servable_here(sub.get("engine")):
        return role, sub, (f"{blocked} — substituting {sub['engine']}: "
                           f"{sub['id']} instead")
    fast = manifest.get("fast")
    if fast and _engine_servable_here(fast.get("engine")):
        return "fast", fast, (f"{blocked} — downshifting to the '{fast.get('engine')}' "
                              f"model {fast.get('id')}")
    return None, None, blocked


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
            role, spec, note = _resolve_auto(tier, manifest)
            if role is None:
                print(f"{WARN} detected tier '{tier}' on {_platform_label()}: "
                      f"{note or 'no locally-servable model fits'}. Use a cloud "
                      f"provider (set inference.backends + AVA_INFERENCE_KEY), or "
                      f"install Ollama/MLX and `ava models pull <role>` by hand.")
                return 0
            if note:
                print(f"{WARN} {note}.\n")
            print(f"{B}--auto{X}: detected tier '{tier}' \u2192 pulling '{role}'\n")
            rc = _pull_one(role, spec, dirs)
            if rc == 0:
                print(f"\n{B}Add to your ava.yaml{X} (if not already configured):\n")
                print(_backend_stanza(role, spec, dirs))
                print()
            return rc
        roles = [args.name] if args.name else list(manifest)
        rc = 0
        for role in roles:
            if role not in manifest:
                print(f"{WARN} no such role '{role}' (have: {', '.join(manifest)})")
                rc = 1
                continue
            rc = _pull_one(role, manifest[role], dirs) or rc
        return rc
    if args.action == "bench":
        from ava_bridge import bench as _bench
        prompt = args.name or _bench.DEFAULT_PROMPT
        only = args.models.split(",") if getattr(args, "models", None) else None
        print(f"\n{B}Model bench{X}  — same prompt on each backend "
              f"(max {args.max_tokens} tokens)\n  prompt: {prompt!r}\n")
        res = _bench.bench(prompt, only=only, max_tokens=args.max_tokens)
        if not res["results"]:
            print(f"{WARN} no matching backends (configure inference.backends in ava.yaml)")
            return 1
        print(f"  {'backend':22} {'TTFT':>8} {'tok/s':>8} {'tokens':>8} {'total':>8}")
        print(f"  {'-'*22} {'-'*8} {'-'*8} {'-'*8} {'-'*8}")
        for r in res["results"]:
            if not r.get("ok"):
                _row(BAD, r["id"][:22], f"error: {r.get('error','')}")
                continue
            star = " *" if r["id"] == res["winner"] else ""
            est = "~" if r.get("estimated_tokens") else " "
            print(f"  {G if r['id']==res['winner'] else ''}{r['id'][:22]:22}{X} "
                  f"{r['ttft_ms']:>7.0f}m {r['tok_s']:>7.1f} {est}{r['tokens']:>6} "
                  f"{r['total_s']:>7.1f}s{star}")
        if res["winner"]:
            print(f"\n{OK} fastest: {B}{res['winner']}{X} (tok/s). "
                  "~ = tokens estimated (endpoint didn't report usage).")
        return 0
    return 1


# --------------------------------------------------------------------------- #
def _prompt_label(default: str | None) -> str | None:
    """Read one label from the owner: Enter accepts the router's suggestion,
    `s` skips, `q` stops mining. Bare letters map to labels for speed."""
    from ava_bridge import eval_router
    shorthand = {"c": "chat", "p": "prompt_help", "n": "image_new",
                 "r": "image_refine"}
    menu = "  ".join(f"[{k}]{v}" for k, v in shorthand.items())
    while True:
        raw = input(f"    label ({menu})  Enter={default or 'chat'} "
                    f"· s=skip · q=quit: ").strip().lower()
        if raw == "":
            return default or "chat"
        if raw == "s":
            return None
        if raw == "q":
            return "__quit__"
        if raw in shorthand:
            return shorthand[raw]
        if raw in eval_router.LABELS:
            return raw
        print(f"    ? unknown label — pick one of {eval_router.LABELS} (or c/p/n/r)")


def cmd_eval(args) -> int:
    from ava_bridge import eval_router, turn_router
    if args.target != "intent":
        print("only `ava eval intent ...` is supported")
        return 2

    if args.action == "run":
        cases = eval_router.load_cases()
        if not cases:
            _row(WARN, "eval set", "empty — build one with `ava eval intent mine`")
            return 0
        print(f"\n{B}Intent eval{X}  ({len(cases)} cases · router "
              f"{turn_router.fingerprint()} · mode {turn_router.mode()})\n")
        s = eval_router.score(cases)
        eval_router.save_result(s)
        acc = s["accuracy"]
        _row(OK if acc and acc >= 0.9 else WARN, "accuracy",
             f"{s['passed']}/{s['total']}  ({acc})")
        _row(WARN if (s["escalation_rate"] or 0) > 0.25 else OK, "escalation rate",
             str(s["escalation_rate"]))
        _row(OK if s["context_distinct"] == s["context_pairs"] else WARN,
             "context sensitivity",
             f"{s['context_distinct']}/{s['context_pairs']} paired texts got "
             "distinct labels")
        if s.get("prompt_checked"):
            _row(OK if not s["prompt_failed"] else BAD, "merged-prompt quality",
                 f"{s['prompt_checked'] - s['prompt_failed']}/{s['prompt_checked']} "
                 "refine prompts matched their include/exclude checks")
        if s["confusion"]:
            print("\n  misroutes (expected->got):")
            for k, n in s["confusion"].items():
                _row(BAD, k, f"x{n}")
        fails = [r for r in s["rows"] if not r["pass"]]
        if fails:
            print("\n  failing cases:")
            for r in fails[:20]:
                _row(BAD, r["id"] or "?",
                     f"{r['expect']} got {r['got']}  · {r['text']}")
        print(f"\n  result saved to {eval_router.result_file()}\n")
        return 0

    if args.action == "run-fingerprint":  # machine-readable for scripts/doctor
        need, why = eval_router.needs_rerun()
        print(json.dumps({"needs_rerun": need, "reason": why,
                          "fingerprint": turn_router.fingerprint()}))
        return 0

    if args.action == "mine":
        cands = eval_router.mine_candidates(getattr(args, "chats", None) or None)
        added = 0
        if cands:
            print(f"\n{B}Mine intent cases{X}  ({len(cands)} new messages from "
                  "your history)\n")
            for cand in cands:
                ctx = cand["context"]
                suggestion = turn_router.classify(
                    cand["text"], ctx.get("last_route"),
                    ctx.get("last_image_prompt"),
                    recent_user=ctx.get("recent_user"))
                cue = (f"  [after {ctx['last_route']}]"
                       if ctx.get("last_route") else "")
                print(f"  {B}{cand['text'][:100]}{X}{cue}")
                print(f"    router suggests: {suggestion['route']} "
                      f"(tier {suggestion['tier']})")
                label = _prompt_label(suggestion["route"])
                if label == "__quit__":
                    break
                if label is None:
                    continue
                eval_router.add_case(
                    cand["text"], label, last_route=ctx.get("last_route"),
                    last_image_prompt=ctx.get("last_image_prompt"),
                    recent_user=ctx.get("recent_user"), source="mined")
                added += 1
        else:
            print("\nNo new messages to label from your history.")
        # Offer hand-typed cases too (a fresh fork with no history still needs
        # coverage; also lets you add edge cases you haven't hit yet).
        print("\nAdd hand-written cases (blank message to finish):")
        while True:
            try:
                text = input("  message: ").strip()
            except EOFError:
                break
            if not text:
                break
            label = _prompt_label("chat")
            if label in (None, "__quit__"):
                break
            eval_router.add_case(text, label, source="manual")
            added += 1
        _row(OK, "saved", f"{added} case(s) -> {eval_router.eval_file()}")
        print("  score them any time with `ava eval intent run`\n")
        return 0
    return 2


def cmd_alloc(args) -> int:
    """Inspect and steer model memory allocation.

    `status` and `plan` are read-only. `restore` brings back what the allocator itself
    released (and nothing else). `reset` clears a model's failure record after you have
    fixed whatever was wrong; `resume` un-quiesces an allocator that hit its own thrash
    guard.
    """
    from ava_bridge import alloc
    from ava_bridge.alloc import breaker

    action = args.action
    if action == "status":
        rep = alloc.report()
        lz = rep.get("leases") or {}
        br = lz.get("breaker") or {}
        print(f"\n{B}Allocation{X}")
        print(f"  mode          : {'ENFORCING' if rep.get('actuating') else 'advisory'}"
              f"{' · evicting' if lz.get('evicting') else ' · eviction off'}")
        pool = rep["pool"]
        if rep["gating"] == "disabled":
            print("  pool          : memory unreadable — not gating")
        else:
            print(f"  pool          : {pool['free_gib']} / {pool['total_gib']} GB free"
                  f" ({pool['source']})"
                  + (f" · baseline {pool['baseline_gib']}"
                     if pool.get("baseline_gib") is not None else "")
                  + (f" · {pool['unknown_gib']} GB undeclared"
                     if pool.get("unknown_gib") else ""))
        if br.get("quiesced"):
            print(f"  {BAD} QUIESCED   : {br.get('quiesce_reason')}")
        print(f"  actions       : {br.get('actions_in_window', 0)}/{br.get('budget')} "
              f"in the last {int((br.get('window_s') or 600) / 60)} min")
        print(f"  leases        : {lz.get('lease_count', 0)} held · "
              f"{len(lz.get('owed') or [])} awaiting restore")
        for od in (lz.get("overdue") or []):
            print(f"  {WARN} overdue    : {od['lease_id']} (pid {od['pid']}) "
                  f"{od['age_s']}s")
        if not rep["models"]:
            print("  declared      : none — add `alloc.models` to ava.yaml to opt in")
        for m in rep["models"]:
            bm = (br.get("models") or {}).get(m["id"]) or {}
            state = ("resident but NOT READY" if m["resident"] and m["ready"] is False
                     else "resident" if m["resident"]
                     else "not resident" if m["resident"] is False else "unknown")
            line = (f"  {m['id']:<16} {m['driver']:<12} {m['priority']:<12} {state}")
            if m.get("resident_gib") is not None:
                line += f" · {m['resident_gib']} GB"
            print(line)
            if bm.get("given_up"):
                print(f"      {BAD} gave up after {bm['fails']} attempts: "
                      f"{bm.get('reason')}")
                print(f"        fix it, then `ava alloc reset {m['id']}`")
            elif bm.get("retry_in_s"):
                print(f"      {WARN} backing off {bm['retry_in_s']}s "
                      f"({bm.get('fails')} failed attempts)")
            elif bm.get("deferred"):
                print(f"      · deferred (not a failure): {bm['deferred']}")
            for prob in m.get("problems") or []:
                print(f"      {WARN} {prob}")
        print()
        return 0

    if action == "plan":
        if not args.model:
            print("usage: ava alloc plan <model-id>")
            return 2
        pl = alloc.admit_plan(args.model)
        print(f"\n{B}Plan for {args.model}{X}")
        print(f"  admit     : {pl.admit}"
              + ("" if pl.gated else "  (memory unknown — not gated)"))
        if pl.need_gib is not None:
            print(f"  need/free : {pl.need_gib:.0f} / "
                  f"{(pl.free_gib if pl.free_gib is not None else 0):.0f} GB"
                  + (f" -> projected {pl.projected_gib:.0f} GB"
                     if pl.projected_gib is not None else ""))
        if pl.shortfall_gib:
            print(f"  shortfall : {pl.shortfall_gib:.0f} GB")
        for s in pl.steps:
            kind = "try (cheap)" if s.speculative else "release"
            print(f"  {kind:<12}: {s.model_id} [{s.mode.value}] — {s.reason}")
        print(f"  reason    : {pl.reason}\n")
        return 0

    if action == "restore":
        ids = alloc.restore_now()
        print("restored: " + (", ".join(ids) if ids else
                              "nothing (either nothing is owed, or enforcement is off)"))
        return 0

    if action == "reset":
        breaker.reset(args.model)
        print(f"breaker cleared for {args.model or 'all models (and QUIESCED)'}")
        return 0

    if action == "resume":
        breaker.reset(None)
        print("allocator resumed: breakers and the action budget are cleared")
        return 0
    return 2


def main() -> int:
    p = argparse.ArgumentParser(prog="ava", description="Ava control CLI")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("doctor", help="check the environment").set_defaults(func=cmd_doctor)
    sub.add_parser("verify", help="end-to-end claim check (connectors, learning, governance, health)").set_defaults(func=cmd_verify)
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
    apn = sub.add_parser("app", help="scaffold the ava-tools/1 agent surface inside YOUR app repo")
    apn.add_argument("action", choices=["new"])
    apn.add_argument("name", nargs="?", help="app id (a-z 0-9 _ -)")
    apn.add_argument("--framework", choices=["fastapi", "flask", "express", "stdlib"],
                     default="fastapi", help="your app's web framework (default fastapi)")
    apn.add_argument("--port", type=int, default=9000, help="the port your app serves on")
    apn.add_argument("--dir", default=".", help="app repo root to write into (default .)")
    apn.add_argument("--ui", action="store_true",
                     help="include the sidebar-tile ui: block (your app serves a web UI)")
    apn.set_defaults(func=cmd_app)
    dp = sub.add_parser("device", help="wire your own device/sensor app to Ava: scaffold / token / events")
    dp.add_argument("action", choices=["list", "new", "token", "events"])
    dp.add_argument("name", nargs="?", help="device connector id (for new / token / events)")
    dp.add_argument("--limit", type=int, default=0, help="max events to show (events)")
    dp.set_defaults(func=cmd_device)
    ap = sub.add_parser("alloc", help="model memory allocation: status / plan / "
                                     "restore / reset / resume")
    ap.add_argument("action", choices=["status", "plan", "restore", "reset", "resume"])
    ap.add_argument("model", nargs="?", help="plan/reset: the declared model id")
    ap.set_defaults(func=cmd_alloc)

    mp = sub.add_parser("models", help="model store: list / pull / verify / bench")
    mp.add_argument("action", choices=["list", "pull", "verify", "bench"])
    mp.add_argument("name", nargs="?",
                    help="pull: role (chat/fast/image); bench: the prompt")
    mp.add_argument("--auto", action="store_true",
                    help="pull the chat/fast model that fits the detected memory tier")
    mp.add_argument("--models", help="bench: comma-separated backend ids/models to compare")
    mp.add_argument("--max-tokens", type=int, default=200, dest="max_tokens",
                    help="bench: completion length per model (default 200)")
    mp.set_defaults(func=cmd_models)
    ep = sub.add_parser("eval", help="build/score your OWN routing eval set (no data ships)")
    ep.add_argument("target", choices=["intent"], help="what to evaluate")
    ep.add_argument("action", choices=["mine", "run", "run-fingerprint"],
                    help="mine: label cases from your history; run: score them")
    ep.add_argument("--chats", help="path to a chats.json to mine (default: this instance's)")
    ep.set_defaults(func=cmd_eval)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
