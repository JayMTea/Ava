#!/usr/bin/env python3
"""Ava control CLI — make installing & running Ava easy.

    ava doctor      # check the environment (hardware, dirs, config, services)
    ava setup       # first-run: create AVA_HOME, generate secrets + password, ava.yaml
    ava up          # run the Ava bridge (web app + API)
    ava version

Designed to be friendly on a fresh machine: `ava setup && ava up` gets a new user
from zero to a running Ava with no source edits. See deploy/README.md.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ava_bridge import settings
from ava_bridge.version import __version__, revision

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


# The whole of a fresh ava.yaml. Deliberately tiny: everything else has a
# working default, and config.example.yaml is where the options are DOCUMENTED.
# Comments here are the only ones that survive, because the first Setup save
# rewrites this file through yaml.safe_dump, which cannot round-trip comments.
_STARTER_CONFIG = """\
# Ava's configuration. Machine-written: the Setup UI rewrites this file, so
# comments you add below will not survive the next save.
#
# Every option Ava understands, with explanations, is in config.example.yaml at
# the repo root — read it there and copy the keys you want into this file.
#
# Anything absent uses a working default, so a short file is normal and correct.

server:
  host: 127.0.0.1        # loopback; see config.example.yaml before widening

setup:
  completed: false       # the first-run wizard flips this
"""


def _server_port() -> int:
    """The port the bridge listens on, from the one resolver.

    Imported lazily: ava_bridge.config creates directories and writes the
    internal token at import, which a plain `ava --help` has no business doing.
    Previously this expression was copied into four call sites, which is how the
    CLI and the bridge came to disagree about the default bind host.
    """
    from ava_bridge import config as _cfg
    return _cfg.SERVER_PORT


# --------------------------------------------------------------------------- #
# Tier recommendation lives in model_fit so doctor / models pull / the setup
# wizard share one source of truth.
from ava_bridge.model_fit import recommend_tier as _recommend_tier  # noqa: E402
from ava_bridge import models  # noqa: E402


def cmd_attest(args) -> int:
    """Evidence bundle: what this box can show about itself, and what it cannot.

    Human summary by default. `--out DIR` is the ONLY thing that writes and has no
    default — no fallback under AVA_HOME — so a plain run disturbs nothing.

    Exit 0 clean / 1 the audit chain is broken / 2 something could not be measured.
    """
    from ava_bridge import attest as _attest

    bundle = _attest.build(redact_biometrics=args.redact_biometrics)
    s = bundle["summary"]

    if args.json:
        print(json.dumps(bundle, indent=2, sort_keys=True))
    else:
        print(f"\n{B}ava attest{X}  {bundle['schema']}  "
              f"({s['artifacts']} artifacts)\n")
        for name, art in sorted(bundle["artifacts"].items()):
            mark = OK if art["state"] == "collected" else WARN
            _row(mark, name, f"{art['state']} · {art['evidence_class']}")
            if art.get("reason"):
                print(f"      {art['reason'][:104]}")
        print()
        if s["complete"]:
            print(f"{OK} every collector answered in full.")
        else:
            print(f"{WARN} INCOMPLETE — {', '.join(s['incomplete'])}")
        print(f"    measurements: {', '.join(s['measurements'])}")
        print(f"    {len(s['not_measured'])} thing(s) a single host cannot attest "
              "to (--json for the reasons)")
        if not args.redact_biometrics:
            vp = ((bundle["artifacts"]["stores"].get("data") or {})
                  .get("stores", {}).get("voiceprint") or {})
            if vp.get("digest"):
                print(f"    {WARN} this bundle contains your voiceprint's digest. "
                      "Pass --redact-biometrics before sharing it.")

    if args.out:
        wrote = _attest.write_bundle(bundle, args.out)
        print(f"    {OK} wrote {len(wrote)} file(s) to {args.out}")
        print(f"      verify it with: cd {args.out} && python3 verify.py . "
              "--self-test")

    chain = bundle["artifacts"]["chain"].get("data") or {}
    if chain.get("state") == "broken":
        return 1
    return 2 if not s["complete"] else 0

def cmd_doctor(_args) -> int:
    print(f"\n{B}Ava doctor{X}  (AVA_HOME = {settings.AVA_HOME})\n")

    print("Environment")
    _row(OK, "Python", sys.version.split()[0])
    _row(OK if settings.CONFIG_PATH.is_file() else WARN, "Config",
         str(settings.CONFIG_PATH) if settings.CONFIG_PATH.is_file()
         else "no ava.yaml yet — run `ava setup`")
    for d in (settings.data_dir(), settings.logs_dir(), settings.secrets_dir()):
        _row(OK if os.path.isdir(d) else WARN, os.path.basename(d) or d, d)

    print("\nHardware")
    try:
        # The platform row first, and its verification tier with it. An owner
        # should learn how well their exact hardware class is actually known
        # from the tool itself, not by finding the matrix in the docs — the
        # honest-labelling rule applied at the moment of first contact.
        from ava_bridge import hwinfo, platforms
        _prow = platforms.detect()
        if _prow is None:
            _row(WARN, "Platform", f"{hwinfo.platform_id()} — no row in "
                                   "deploy/platforms.conf")
        else:
            _mark = OK if _prow.verified else (
                BAD if _prow.tier == "unsupported" else WARN)
            _row(_mark, "Platform", _prow.summary())
    except Exception as e:  # noqa: BLE001 — never let the matrix break doctor
        _row(WARN, "Platform", f"unreadable ({e})")
    try:
        from ava_bridge import hardware
        g = hardware._gpu()
        if g.get("name"):
            _row(OK, "GPU", f"{g['name']} · {g.get('temp','?')}°C · {g.get('power','?')}W")
        else:
            # Derived from the HAL rather than hardcoded to one vendor: this line
            # said "no NVIDIA GPU detected" on any non-NVIDIA machine, so a
            # working AMD or Intel box got a warning about hardware it has.
            from ava_bridge import hwinfo
            plat = hwinfo.platform_id()
            fit = hwinfo.fit_memory()
            if fit.readable:
                _row(OK, "GPU", f"no per-GPU telemetry on {plat} — "
                                f"fit gates on {fit.source} "
                                f"({fit.free_gb:.0f} GB free)")
            else:
                _row(WARN, "GPU", f"no readable accelerator on {plat} — "
                                  "use the cpu/cloud profile")
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
        # An operator-stated pool changes the RECOMMENDATION and not the reading
        # above, so doctor prints both. Showing only the stated number would hide
        # the disagreement that matters; showing only the measured one would
        # contradict what Setup displays.
        try:
            from ava_bridge import hwinfo as _hw
            stated = _hw.stated_fit_gb()
        except Exception:  # noqa: BLE001
            stated = None
        if stated is not None:
            tier, hint = _recommend_tier(stated)
            over = avail is not None and stated > avail * 1.05
            _row(WARN if over else OK, "you set",
                 f"{stated:g} GB — Ava plans for this instead of what it measures"
                 + (f". That is MORE than the {avail:.0f} GB measured here: if your "
                    "models run in this container they will be killed on load, not "
                    "refused." if over else ""))
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
        # A drop-in driver that failed to import used to vanish silently: the
        # model fell to the observe floor, which is safe but indistinguishable
        # from a typo in `driver:`. docs/ALLOCATION.md promised this row long
        # before it existed.
        for err in rep.get("driver_errors") or []:
            _row(WARN, f"driver {err.get('file', '?')}", err.get("error", ""))
            if err.get("traceback"):
                for line in str(err["traceback"]).splitlines()[-2:]:
                    print(f"      {line.strip()}")
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
        # WHICH model answers a turn is one question with one answer, and this
        # is the last surface that was still working it out for itself: it read
        # the sandbox directly, so it printed a brain row when the agent runtime
        # had one and NO row at all when the brain came from ava.yaml — silent
        # about the commonest case. `alloc/__init__.py` refuses a second
        # derivation here for exactly this reason.
        from ava_bridge import models as _models
        brain = _models.effective_brain()
        _origin = {"agent": "sandbox — via `nemoclaw onboard`",
                   "configured": "configured in ava.yaml",
                   "implicit": "from AVA_BACKEND_URL"}.get(brain.get("source", ""), "")
        if brain.get("source") == "none":
            _row(WARN, "brain", "not set — connect one in Setup -> Agent -> Brain")
        else:
            _row(OK, "brain",
                 f"{brain.get('label') or brain.get('model_id') or 'name pending'}"
                 f"{f' ({_origin})' if _origin else ''}")
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
    # Can chat actually answer? Doctor used to return 0 no matter what, so the
    # documented `ava setup && ava doctor && ava up` chain sailed past "nothing
    # serves this model" and handed the user a chat box that 503s on the first
    # message. Track it and exit non-zero so the chain stops where the problem is.
    inference_ok = False
    if not backends:
        # "None configured" is only a problem when nothing else serves. With an
        # onboarded agent sandbox, chat thinks with the sandbox model and the
        # yaml block is an optional fallback — say so instead of warning.
        #
        # Asked of the one resolver rather than the sandbox directly, so this
        # agrees with the `brain` row above by construction.
        brain_src = ""
        try:
            from ava_bridge import models as _m
            _b = _m.effective_brain()
            brain_src = str(_b.get("source") or "none")
            brain_name = str(_b.get("label") or _b.get("model_id") or "")
        except Exception:  # noqa: BLE001
            brain_name = ""
        if brain_src not in ("", "none"):
            inference_ok = True
            _row(OK, "backends",
                 f"none in ava.yaml — chat thinks with the agent sandbox model "
                 f"({brain_name or 'name pending'}); a yaml backend is an "
                 f"optional fallback")
        else:
            # Expected on a fresh install: Ava ships no default model, so there
            # is nothing here until the owner connects one. Still BAD, because
            # chat genuinely cannot answer and `ava setup && ava doctor && ava
            # up` must stop here rather than hand over a chat box that 400s.
            _row(BAD, "backends", "no model connected yet — nothing can answer "
                 "a chat turn. Connect one in Setup -> Agent -> Brain (or run "
                 "`ava setup`), then re-run `ava doctor`")
    for name, b in backends.items():
        b = b or {}
        url = b.get("base_url", "")
        ok = _probe(url.rstrip("/") + "/models") if url else False
        inference_ok = inference_ok or ok
        # Flag a backend whose declared weight won't fit the detected memory.
        weight = ((b.get("fit") or {}).get("weight_gb"))
        note = f"{url}  {'up' if ok else 'unreachable'}"
        icon = OK if ok else WARN
        if weight and avail and float(weight) > float(avail):
            icon = WARN
            note += f"  ! needs ~{weight} GB > {avail:.0f} GB available"
        _row(icon, name, note)

    # What Ava can FIND, as opposed to what ava.yaml declares — and crucially,
    # WHERE. This is the only check that can answer "does host.docker.internal
    # resolve and answer on this machine", and it has to run on the machine in
    # question: the discovery path is gated on being in a container and on the
    # runtime publishing a host gateway, so neither a unit test nor a run on the
    # maintainer's Linux box can settle it for someone else's laptop.
    print("\nEngine discovery (where Ava can find one)")
    try:
        from ava_bridge import setup_wizard as _sw
        gw = _sw.host_gateway()
        in_ctr = _sw._in_container()
        if not in_ctr:
            _row(OK, "container", "not in a container — this box's loopback IS "
                                  "the machine, so there is no host to reach")
        elif gw:
            # Resolving is not reaching. Saying "an engine on the machine is
            # reachable from in here" contradicted the very next row on a box
            # that filters container->host traffic, which is the case this
            # section exists to diagnose.
            _row(OK, "host gateway", f"{gw} resolves — Ava knows how to address "
                                     "the machine it runs on")
        else:
            _row(WARN, "host gateway",
                 "none of host.docker.internal / host.containers.internal "
                 "resolves — an engine running on the machine outside this "
                 "container cannot be found. On Linux, compose needs "
                 "`extra_hosts: [\"host.docker.internal:host-gateway\"]`.")
        found = _sw.api_backends()
        for b in found["backends"]:
            if not b["up"]:
                continue
            inference_ok = True
            _row(OK, b["engine_label"], f"{b['base_url']}  ({b['locality']})")
        if not any(b["up"] for b in found["backends"]):
            reach = found.get("host_reach") or ""
            if reach == "dropped":
                _row(BAD, "discovered", "nothing answered, and the machine "
                     "itself is unreachable from in here — packets are being "
                     "dropped, not refused. Something is filtering between this "
                     "container and the host; on Windows, Defender Firewall on "
                     "the WSL vEthernet adapter. A bind address will not fix it.")
            elif reach == "refused":
                _row(WARN, "discovered", "nothing answered. The machine IS "
                     "reachable, so an engine running on it is bound to "
                     "127.0.0.1 only — it must listen wider to be reached from a "
                     "container (Ollama: OLLAMA_HOST=0.0.0.0, then restart it).")
            else:
                _row(WARN, "discovered", "nothing answered on any known port")
    except Exception as e:  # noqa: BLE001 — discovery must never break doctor
        _row(WARN, "engine discovery", f"probe failed: {e}")

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

    print("\nBridge")
    port = _server_port()
    _row(OK if _probe(f"http://127.0.0.1:{port}/api/health") else WARN,
         "web app", f"http://127.0.0.1:{port}")
    print()

    if not inference_ok:
        # Exit 2, not 1: the report itself succeeded, the box is not ready. The
        # named command is the missing step — `ava up` starts the WEB APP, never
        # an inference engine, and nothing else in the install docs says so.
        print(f"{BAD} No inference backend is reachable, so chat cannot answer yet.")
        print("   Start an engine, then re-run this check:")
        print("     ava models pull --auto          # download a model, once")
        print("     bash deploy/local-serve.sh      # NVIDIA + Docker: serve it with vLLM")
        print("     # or on Apple Silicon / CPU:  ollama serve  &&  ollama pull <model>")
        print("     ava doctor\n")
        return 2
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

    # ava.yaml starter — MINIMAL, not a copy of the annotated template.
    #
    # This used to `shutil.copyfile(config.example.yaml, ava.yaml)`, which caused
    # two separate defects:
    #
    #   * The template is 370+ lines of explanatory comments, and yaml.safe_dump
    #     cannot round-trip comments. So the FIRST Setup toggle rewrote the file
    #     and silently stripped every one of them — the documentation was
    #     imported into the user's config purely so it could be destroyed.
    #   * The template ships a live `inference.backends.local`, and
    #     setup_wizard.setup_completed() treats "any declared backend" as "already
    #     onboarded" — so every CLI-setup install skipped the first-run wizard
    #     entirely, without ever showing it.
    #
    # config.example.yaml is documentation. ava.yaml is machine-written. Keeping
    # them separate is what makes both statements true.
    created = False
    if not settings.CONFIG_PATH.is_file():
        try:
            settings.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
            settings.CONFIG_PATH.write_text(_STARTER_CONFIG, encoding="utf-8")
            os.chmod(settings.CONFIG_PATH, 0o600)
            created = True
            _row(OK, "ava.yaml", f"created at {settings.CONFIG_PATH}")
        except OSError as e:
            _row(WARN, "ava.yaml", f"skip: {e}")
    else:
        _row(OK, "ava.yaml", "already exists")

    # The shipped default backend serves the `chat` model on vLLM. Reseed the fresh
    # inference block whenever the model that actually fits THIS box is a different
    # one, so setup never leaves a user pointing at an endpoint they can't serve:
    #   * Apple Silicon / CPU-only — vLLM doesn't run there at all.
    #   * A small NVIDIA GPU — `_resolve_auto` downshifts to the Ollama 'fast' model
    #     (so `models pull --auto` fetches that), but the inference block would still
    #     name the vLLM default. The user then downloads one model while Ava talks to
    #     a different, dead endpoint. Gating on the platform missed this case.
    # Best-effort throughout: any failure leaves the shipped example in place.
    if created:
        try:
            tier, _avail = _detected_tier()
        except Exception:  # noqa: BLE001
            tier = None
        spec, why = _inference_reseed(_models_manifest(), tier)
        if spec:
            if _seed_inference_backend(spec):
                _row(OK, "inference",
                     f"seeded {spec['engine']} backend ({spec['id']}) for "
                     f"{_platform_label()} — {why}")
        elif why:
            _row(WARN, "inference", why)

    # model store scaffolding — same hf/ollama layout as the Docker volumes,
    # so a fork's tree matches the author's and `ava models pull` has a home.
    dirs = ensure_model_dirs()
    _row(OK, "model dirs", f"{dirs['root']}/{{hf,ollama}}")

    # connectors/ is where every example tells a forker to copy a manifest, and
    # setup did not create it — so the FIRST thing anyone tries after chat works
    # ("cp -r examples/device-app $AVA_HOME/connectors/device-demo") died with "No such
    # file or directory". Six example READMEs and three docs all carried that same
    # broken line. Creating it here is the fix; the mkdir -p those docs now show is
    # belt-and-braces for an AVA_HOME that predates this.
    _conn = settings.home("connectors")
    os.makedirs(_conn, exist_ok=True)
    _row(OK, "connectors dir", _conn)

    # Keep these four steps in sync with README.md and deploy/README.md —
    # tests/test_install_steps.py fails if any of them drifts. The engine step is
    # the one that used to be missing everywhere: `ava up` starts the web app and
    # never an inference engine, so the three-step version handed the user a chat
    # box that 503s on the first message.
    print(f"\n{G}Setup complete.{X} Next:")
    print("  ava models pull --auto        # download a model that fits this box")
    print("  bash deploy/local-serve.sh    # serve it (Apple Silicon/CPU: ollama serve)")
    print("  ava doctor                    # exits non-zero if nothing can answer")
    print("  ava up                        # start the web app\n")
    return 0


# --------------------------------------------------------------------------- #
def cmd_up(args) -> int:
    # Resolved by ava_bridge.config, not re-derived here. This line used to
    # default to "0.0.0.0" while config.py defaulted to "127.0.0.1"; `ava up`
    # is what most people run, so the wider bind is the one that shipped —
    # against SECURITY.md's own claim and ava_security_check.py's own check.
    from ava_bridge import config as _cfg
    host = args.host or _cfg.SERVER_HOST
    port = args.port or _cfg.SERVER_PORT
    py = sys.executable
    print(f"{B}Starting Ava{X} on http://{host}:{port}  (Ctrl-C to stop)\n")
    if host not in ("127.0.0.1", "::1", "localhost"):
        from ava_bridge import auth as _auth
        if _auth.needs_setup():
            print(f"{Y}●{X} Binding {host} with NO admin password set. Until you set one, "
                  "anyone who can reach this port could claim this Ava.")
            print("  Setup from another machine now requires the claim token printed "
                  "below; from this machine, just open the URL above.\n")
    cmd = [py, "-m", "uvicorn", "phone_bridge:app", "--host", str(host),
           "--port", str(port),
           # See serve.py: only trusted peers may assert scheme/address.
           "--proxy-headers",
           "--forwarded-allow-ips", ",".join(_cfg.TRUSTED_PROXIES)]
    # Tell the bridge which port it is actually being bound to. Without this,
    # `ava up --port 9455` binds 9455 while the child re-reads server.port from
    # ava.yaml, so the first-run banner prints the CLAIM URL on the configured
    # port: measured as "open http://localhost:8096/setup?claim=…" from a bridge
    # listening on 9455. The one URL a new owner is told to click, pointing at a
    # port with nothing on it — or, on this box, at a different Ava entirely.
    env = dict(os.environ)
    if args.port:
        env["AVA_PORT"] = str(port)
    if args.host:
        env["AVA_HOST"] = str(host)
    return subprocess.call(cmd, cwd=settings.CODE_ROOT, env=env)


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
    pol_dir = settings.generated_policy_dir()
    tool_root = settings.connector_tools_dir()
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

    # 1b. Agent runtime — does what we render actually REACH the sandbox?
    #
    # The section above proves the generator is honest; it says nothing about the
    # sandbox. That gap is how three separate bugs shipped at once: server drift
    # compared a tree fold against an entry-point digest so no Apply could ever
    # report success, the generated files were written to the code root where a
    # container rebuild discarded them, and install.sh resolved a different
    # sandbox name than the bridge whenever `agent.sandbox` came from ava.yaml.
    # Every one of them is invisible until you look inside the sandbox, so this
    # looks.
    #
    # Every row here is a WARN when there is no runtime to ask: a fresh fork, a
    # CPU-only box and CI all legitimately have none, and a hard failure there
    # would train people to ignore the whole report.
    print("\nAgent runtime  (does what we render reach the sandbox?)")
    from ava_bridge import provision, runtime, settings as _settings
    rt = runtime.configured()
    _row(OK if config.AGENT_ENABLED else WARN, "agent.enabled",
         f"runtime={config.AGENT_RUNTIME}" if config.AGENT_ENABLED
         else "off — tool-less direct chat only")
    # A misspelled `agent.runtime` still falls back to NemoClaw — a typo must not
    # brick the box — but silently running a different runtime than the one asked
    # for is not a degradation, it is a disagreement.
    name_err = runtime.name_error()
    _row(BAD if name_err else OK, "agent.runtime", name_err or "a known runtime")
    fails += bool(name_err)
    # `agent.required: true` with `agent.runtime: direct` cannot both be honoured.
    gate_err = runtime.gate()[1]
    if gate_err:
        _row(BAD, "agent.required", gate_err.split(" — ")[0])
        fails += 1

    # The generated trees must be somewhere both the bridge and install.sh read.
    state_root = _settings.agent_state_dir()
    legacy = os.path.join(settings.CODE_ROOT, "agent")
    stranded = []
    if os.path.realpath(state_root) != os.path.realpath(legacy):
        for parts in _settings.GENERATED_AGENT_TREES:
            old = os.path.join(legacy, *parts)
            if os.path.isdir(old) and os.listdir(old):
                stranded.append(os.path.join(*parts))
    _row(BAD if stranded else OK, "generated root",
         f"{len(stranded)} tree(s) still under the code root: {stranded} — "
         "restart the bridge to migrate" if stranded
         else os.path.relpath(state_root, _settings.AVA_HOME) + " (under AVA_HOME)")
    fails += bool(stranded)

    if not rt.available():
        _row(WARN, "sandbox", getattr(rt, "live", lambda: {})().get("reason")
             or "no agent runtime — `ava agent provision --install`")
    else:
        live = rt.live()
        _row(OK if live.get("live") else WARN, "sandbox",
             f"{getattr(rt, 'sandbox', '?')} is live" if live.get("live")
             else live.get("reason") or "not running")

        # install.sh reads its identity from the environment ALONE, so the one
        # thing worth asserting is that the environment we hand it names the
        # sandbox the bridge just inspected.
        if hasattr(rt, "install_env"):
            env = rt.install_env()
            agrees = env.get("AVA_OC_SANDBOX") == getattr(rt, "sandbox", None)
            _row(OK if agrees else BAD, "bridge <-> install.sh",
                 f"both mean '{env.get('AVA_OC_SANDBOX')}'" if agrees
                 else "install.sh would deploy into a DIFFERENT sandbox")
            fails += (not agrees)

        if live.get("live"):
            st = provision.state(rt=rt, force=True)
            for scope in provision.SCOPES:
                sc = st["scopes"][scope]
                mark = {"deployed": OK, "unknown": WARN}.get(sc["state"], BAD)
                _row(mark, f"  {scope}",
                     f"{sc['state']} ({sc['counts']['total']} item(s))"
                     + (f" — {sc['pending']} pending" if sc["pending"] else ""))
            _row(BAD if st["pending"] else OK, "apply state",
                 f"{st['pending']} change(s) not in the sandbox — "
                 "`ava agent provision`" if st["pending"]
                 else "everything this checkout declares is deployed")
            fails += bool(st["pending"])

            # The other direction, which nothing looked in until now. A WARN
            # rather than a failure: `policy-add` has no remove verb, so the
            # only cure for a stranded allowance is a sandbox rebuild, and
            # failing a check whose fix is that heavy would just get ignored.
            extra = {k: v for k, v in (st.get("orphans") or {}).items() if v}
            _row(WARN if extra else OK, "sandbox extras",
                 ", ".join(f"{k}: {', '.join(v)}" for k, v in extra.items())
                 + "  (not declared here; clears on `nemoclaw "
                   f"{getattr(rt, 'sandbox', '<name>')} rebuild`)" if extra
                 else "nothing in the sandbox this checkout does not declare")

    # Generated files with no connector behind them. Hard, and local: these are
    # tarred into the sandbox on the next provision, where no policy allows their
    # routes — so Ava is handed tools that deny-by-default guarantees will fail.
    orphaned = connectors.orphans()
    n_orphan = len(orphaned["tools"]) + len(orphaned["policies"])
    _row(BAD if n_orphan else OK, "orphaned files",
         f"{n_orphan} generated file set(s) with no connector "
         f"({', '.join(orphaned['tools'] + orphaned['policies'])}) — "
         "`ava agent prune --write`" if n_orphan
         else "every generated file has a manifest behind it")
    fails += bool(n_orphan)

    stranded_state = _settings.stranded_agent_state()
    if stranded_state:
        _row(WARN, "stranded state",
             f"{len(stranded_state)} file(s) at the legacy code-root path — "
             "`ava agent adopt-state --write`")

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
    port = _server_port()
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


def cmd_claim(args) -> int:
    """Print the first-run claim token and the URL that uses it.

    `deploy/install.sh` prints this at the end of an install, but the documented
    `cp profiles/<p>.env .env && docker compose up -d` path never runs it — and
    that owner then meets the claim gate with nothing to type into it. Reading
    the file by hand needs the container path and, on Windows, an MSYS opt-out;
    both are things the owner should not have to know.
    """
    from ava_bridge import auth, config

    if not auth.needs_setup():
        print("This instance is already claimed — a password is set. "
              "Sign in at /login.")
        return 0
    token = auth.claim_token()
    if not token:
        # claim_token() returns "" when AVA_HOME is unwritable; it refuses to
        # block setup on bookkeeping, so say what that means rather than
        # printing an empty line and exiting 0.
        print(f"No claim token could be written under {config.CHATS_DIR!r}. "
              "Setup is not gated by a token in that state; if /setup still "
              "refuses you, fix the permissions on that directory.", file=sys.stderr)
        return 1
    base = (args.url or "http://localhost:8096").rstrip("/")
    link = f"{base}/setup?claim={token}"
    print(f"Token: {token}")
    print(f"Open:  {link}")
    print("The link is single-use: it stops working the moment a password is set.")
    if not args.no_browser and _can_open_browser():
        import webbrowser
        webbrowser.open(link)
    return 0


def _can_open_browser() -> bool:
    """Is there a desktop session on THIS machine to open into?

    Same three refusals as deploy/install.sh's open_browser(): over SSH the
    browser would open on the wrong machine, CI has no session at all, and a
    headless Linux box has no display. webbrowser.open() returns True on some of
    these after launching a text-mode browser into the terminal, which is worse
    than doing nothing, so the check is made here rather than trusting it.
    """
    if os.environ.get("CI"):
        return False
    if any(os.environ.get(k) for k in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return False
    if sys.platform.startswith("linux"):
        return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    return True


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
    if action == "adopt-state":
        # Explicit and dry-run by default, on purpose: this moves files that live
        # OUTSIDE this AVA_HOME. Wired into a boot step instead, it moved the
        # primary install's generated tools into whatever home the importing
        # process happened to have. See settings.migrate_agent_state().
        from ava_bridge import settings as _s
        planned = _s.migrate_agent_state(write=args.write)
        if not planned:
            print(f"\n{OK} nothing stranded at the legacy path "
                  f"({os.path.join(str(_s.CODE_ROOT), 'agent')}).\n")
            return 0
        verb = "adopted" if args.write else "would adopt"
        print(f"\n{B}{verb.capitalize()} into {_s.agent_state_dir()}{X}")
        for rel in planned:
            print(f"  {OK if args.write else '·'} {rel}")
        if not args.write:
            print("\nRe-run with --write to move them. "
                  "Anything already present at the destination is left alone.\n")
        else:
            print()
        return 0
    if action == "prune":
        from ava_bridge import connectors as _c
        res = _c.prune_orphans(write=args.write)
        total = len(res["tools"]) + len(res["policies"])
        if not total:
            print(f"\n{OK} no generated files without a connector.\n")
            return 0
        verb = "removed" if args.write else "would remove"
        print(f"\n{B}Generated material with no connector ({verb}){X}")
        for cid in res["tools"]:
            print(f"  {OK if args.write else '·'} tools     {cid}")
        for cid in res["policies"]:
            print(f"  {OK if args.write else '·'} policy    {cid}")
        if not args.write:
            print("\nThese ship into the sandbox on the next provision, where no "
                  "policy allows their routes —\nAva gets tools that are "
                  "guaranteed to fail. Re-run with --write to remove them.\n")
        else:
            print()
        return 0
    if action == "provision":
        scope = getattr(args, "only", "all") or "all"
        from ava_bridge import provision as provision_mod
        if scope not in provision_mod.ALL_SCOPES:
            print(f"{BAD} unknown scope {scope!r} "
                  f"(want: {', '.join(provision_mod.ALL_SCOPES)})")
            return 2
        what = "the agent runtime (NemoClaw)" if scope == "all" else f"{scope}"
        print(f"{B}Provisioning {what}…{X}")
        # `configured()`, not `nemoclaw()`: on `agent.runtime: remote` the local
        # CLI is the wrong machine entirely.
        res = runtime.configured().provision(auto_install=args.install, scope=scope)
        for s in res.get("steps", []):
            print(f"  {OK if s['ok'] else BAD} {s['step']}: {s['detail']}")
        print(f"\n{OK if res['ok'] else WARN} {res['detail']}")
        return 0 if res["ok"] else 1
    print(f"{BAD} usage: ava agent status | provision [--install] [--only SCOPE]")
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


def _resolve_connector_ids(connectors, name):
    """The ids to act on, or None if a NAMED connector does not exist.

    `ids = [name] if name else all` meant a typo produced an empty result and a
    cheerful "0 tool(s) written — run `cd agent && ./install.sh` to deploy", which
    reads as a completed deploy step. A forker who fat-fingers a name during setup
    believed they had generated and deployed tools that were never generated.
    """
    known = [m["id"] for m in connectors.all()]
    if not name:
        return known
    if name not in known:
        print(f"{BAD} no such connector: {name}")
        if known:
            near = [k for k in known if k.startswith(name[:3])] or known
            print(f"   known: {', '.join(sorted(known))}")
            if near and near != known:
                print(f"   did you mean: {', '.join(sorted(near))}?")
        else:
            print("   none are installed — see `ava connector new <name>`")
        return None
    return [name]


def _why_no_tools(connectors, cid: str) -> str:
    """Why `ava connector tools <cid>` emitted nothing.

    Four of seven shipped connectors generate no tools, and the CLI printed a
    bare blank screen for all four — indistinguishable from a crash. The old
    note was gated on whether ANY connector in the registry declared actions, so
    one connector that did muted the explanation for every connector that
    didn't.
    """
    acts = [a for a in connectors.actions() if a.get("connector") == cid]
    if not acts:
        return ("the manifest declares no `actions:` — nothing to generate. "
                "Health probes and egress still work")
    named = ", ".join(a["id"] for a in acts if a.get("id")) or "unnamed"
    return (f"its {len(acts)} action(s) ({named}) declare no `path:`, so they "
            "are not generic-proxy actions. A generated tool calls "
            f"/internal/connector/{cid}/<action>, which needs one — an action "
            "served by a built-in Ava tool instead is correct without it")


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
        # Validate BEFORE joining. `os.path.join(home, name)` with an unchecked
        # name is a path traversal: `ava connector new ../../../tmp/x` wrote the
        # manifest outside AVA_HOME entirely and still printed "+ created". A
        # space in the id was equally accepted and produced tools whose fetch path
        # carried a raw space. `ava app new` already refused both through
        # scaffold._ID_RE; reuse that rather than add a second, drifting rule.
        from ava_bridge.scaffold import valid_id
        if not valid_id(args.name):
            print(f"{BAD} id must be 2-32 chars: a-z 0-9 _ - (starting with a letter)")
            return 1
        d = os.path.join(settings.home("connectors"), args.name)
        path = os.path.join(d, "connector.yaml")
        if os.path.exists(path):
            print(f"{WARN} already exists: {path}")
            return 1
        os.makedirs(d, exist_ok=True)
        # COPY the reference template rather than emitting a second one. The
        # inline copy that used to live here had drifted in two ways that both
        # only bite after you follow its own invitation to uncomment something:
        #   * its sample action had no `path:`, and both connectors.tool_files()
        #     and render_egress_policy() require one — so `ava connector tools`
        #     and `ava connector policies` printed NOTHING, with no error.
        #   * its perf path pointed inside $AVA_HOME/connectors/<id>/, which the
        #     reference template explicitly warns against because removing the
        #     connector then destroys its Vitals history.
        # One source of truth makes that divergence structurally impossible.
        src = os.path.join(settings.CODE_ROOT, "connectors", "_template",
                           "connector.yaml")
        try:
            body = open(src, encoding="utf-8").read()
            body = body.replace("id: myapp", f"id: {args.name}")
            body = body.replace("label: My App", f"label: {args.name}")
            body = body.replace("myapp", args.name)
        except OSError:
            # A trimmed fork with no connectors/ dir still gets something valid.
            body = _CONNECTOR_TEMPLATE.replace("NAME", args.name)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"{OK} created {path}")
        print("   edit it, then restart Ava (or `ava up`) to load the connector.")
        print("   check what it will generate, before deploying:")
        print(f"     ava connector tools {args.name}")
        print(f"     ava connector policies {args.name}")
        return 0
    if args.action == "policies":
        import yaml as _yaml
        ids = _resolve_connector_ids(connectors, args.name)
        if ids is None:
            return 1
        wrote = 0
        for cid in ids:
            pol = connectors.render_egress_policy(cid)
            if not pol:
                if not args.write:
                    print(f"# ---- {cid} ----\n"
                          f"  (no egress policy: the manifest declares no "
                          f"`egress:` block and nothing that derives one)")
                continue
            text = _yaml.safe_dump(pol, sort_keys=False)
            if args.write:
                outdir = settings.generated_policy_dir()
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
        ids = _resolve_connector_ids(connectors, args.name)
        if ids is None:
            return 1
        wrote = 0
        for cid in ids:
            # tool_files decides the shape: find/call meta tools for dynamic or
            # large static connectors, else one tool per generic-proxy action.
            files = connectors.tool_files(cid)
            if not files and not args.write:
                print(f"# ---- {cid} ----\n  (no tools to generate: "
                      f"{_why_no_tools(connectors, cid)})")
            for t in files:
                if args.write:
                    outdir = settings.connector_tools_dir(cid)
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
        # Same traversal hole as `connector new` — same fix, same regex.
        from ava_bridge.scaffold import valid_id
        if not valid_id(args.name):
            print(f"{BAD} id must be 2-32 chars: a-z 0-9 _ - (starting with a letter)")
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
        port = _server_port()
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
#
# _DEFAULT_MODELS is a STARTING POINT, not a requirement. Ava runs on any model its
# engines can serve; these ids are only what a fresh `ava setup` seeds when the user
# hasn't chosen yet. The user's `models:` block in ava.yaml overlays this (see
# _models_manifest), and Setup → Models in the UI rewrites the inference backend
# outright. Changing a default here changes what NEW installs pull — nothing else.
# Moved to ava_bridge/models.py so the bridge and the CLI share ONE
# implementation. hub_api used to sys.path-inject the code root and import this
# script to reach these as private functions; it now imports the module.
# The underscore aliases below are kept so existing call sites (and the tests
# that pin this behaviour) keep working unchanged.
_DEFAULT_MODELS = models.DEFAULT_MODELS
_OLLAMA_CHAT = models.OLLAMA_CHAT
_models_manifest = models.manifest
_model_dirs = models.dirs
ensure_model_dirs = models.ensure_dirs


_hf_present = models.hf_present


def _pull_hf(model_id: str, hf_dir: str) -> int:
    env = {**os.environ, "HF_HOME": hf_dir}
    for exe in ("hf", "huggingface-cli"):
        if shutil.which(exe):
            cmd = [exe, "download", model_id]
            print(f"  $ HF_HOME={hf_dir} {' '.join(cmd)}")
            return subprocess.call(cmd, env=env)
    print(f"  {WARN} huggingface CLI not found — pip install huggingface_hub")
    return 1


_ollama_env = models.ollama_env
_LOCAL_CHAT_ENGINES = models.LOCAL_CHAT_ENGINES


_ollama_present = models.ollama_present


def _pull_ollama(tag: str, ollama_dir: str) -> int:
    if not shutil.which("ollama"):
        print(f"  {WARN} ollama not installed — https://ollama.com/download")
        return 1
    print(f"  $ OLLAMA_MODELS={ollama_dir} ollama pull {tag}")
    return subprocess.call(["ollama", "pull", tag], env=_ollama_env(ollama_dir))


def _gguf_target(spec: dict, gguf_dir: str) -> str:
    name = os.path.basename(spec.get("id") or spec.get("url") or "model.gguf")
    return os.path.join(gguf_dir, name)


_gguf_present = models.gguf_present


def _pull_gguf(spec: dict, gguf_dir: str) -> int:
    """Direct-URL GGUF download for llama.cpp — a plain streaming download."""
    return _download_url(spec.get("url"), _gguf_target(spec, gguf_dir),
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


_model_present = models.present


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


_detected_tier = models.detected_tier


_platform_label = models.platform_label


def _inference_reseed(manifest: dict, tier: str | None) -> tuple[dict | None, str | None]:
    """Should `ava setup` replace the shipped vLLM default backend, and why?

    Returns `(spec, reason)` when the model that actually fits this box differs
    from the shipped `chat` default AND is locally servable — the caller writes it
    to ava.yaml. Returns `(None, warning)` when nothing local fits at all, and
    `(None, None)` when the shipped default is already the right choice.

    Two distinct cases reach the first branch, and both used to be missed by
    gating on the platform alone:
      * vLLM can't run here (Apple Silicon, CPU-only) — substitute an engine
        that can.
      * vLLM runs, but this GPU is too small for the full-size default, so
        `_resolve_auto` downshifts to the 'fast' role. Without a reseed the user
        downloads the small model while ava.yaml still names the big one.
    """
    if not tier:
        return None, None
    try:
        _role, spec, _note = _resolve_auto(tier, manifest)
    except Exception:  # noqa: BLE001 — never let detection break setup
        return None, None
    if spec is None:
        return None, (f"no local engine fits {_platform_label()}; configure a "
                      "cloud backend (see config.example.yaml) or install Ollama/MLX")
    default_chat = manifest.get("chat") or {}
    same = (spec.get("id") == default_chat.get("id")
            and spec.get("engine") == default_chat.get("engine"))
    if same or spec.get("engine") not in _LOCAL_CHAT_ENGINES:
        return None, None
    why = ("the vLLM default won't run here"
           if not _engine_servable_here(default_chat.get("engine"))
           else f"the default is too large for this box (tier: {tier})")
    return spec, why


def _seed_inference_backend(spec: dict) -> bool:
    """Rewrite ava.yaml's `inference` block to a single servable local backend
    matching `spec` (engine + model), replacing the vLLM default. Best-effort:
    returns True only if it rewrote the file. Called from `ava setup` whenever the
    model that fits this box differs from the shipped default — a Mac never boots
    pointing at a dead vLLM endpoint, and a small NVIDIA GPU is pointed at the
    downshifted model it actually downloaded rather than the full-size default."""
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


_engine_servable_here = models.engine_servable_here


_resolve_auto = models.resolve_auto


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
                # Non-zero: NOTHING WAS DOWNLOADED. This returned 0, so every
                # caller that checks an exit code — the installer, a script, and
                # the Hub's model-pull job, which renders `done` — was told the
                # pull succeeded. A box with no servable model then reached the
                # first chat turn believing it had one.
                return 1
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


def cmd_alloc(args) -> int:
    """Inspect and steer model memory allocation.

    `status` and `plan` are read-only. `release` frees one declared model's memory
    because you said so — it works whether or not `alloc.lease.enforce` is on, because
    that switch governs whether Ava may act on its own judgement, and this is yours.
    Anything you release stays down until you bring it back: nothing autonomous will
    undo your decision. `restore` brings back what was released — a model id for one,
    or bare for everything owed. `reset` clears a model's failure record after you have
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

    if action == "release":
        if not args.model:
            print("usage: ava alloc release <model-id>")
            return 2
        r = alloc.owner_release(args.model, mode=args.mode)
        for line in r.get("log") or []:
            print(f"  {line}")
        if not r["ok"]:
            print(f"{BAD} {args.model} was not released ({r['code']})"
                  + (f": {r['detail']}" if r.get("detail") else ""))
            if r["code"] == "held_live":
                print("  something is using it right now — Ava does not take memory "
                      "from work in progress")
            if r["code"] == "observe_only":
                print("  no release lever is declared for it — see docs/ALLOCATION.md")
            return 1
        freed = r.get("freed_gib")
        if freed is not None:
            print(f"{OK} freed {freed} GB from {args.model} [{r.get('mode')}] "
                  f"({r.get('free_before_gib')} -> {r.get('free_after_gib')} GB free)")
        else:
            # Never print 0. `None` here means this box cannot measure the delta, and
            # "freed 0 GB" would read as "nothing happened", which is a different claim.
            print(f"{OK} {args.model} released [{r.get('mode')}] — this machine cannot "
                  "measure how much came back")
        print(f"  it stays down until you run `ava alloc restore {args.model}`")
        return 0

    if action == "restore":
        # `owner=True`: this is a person at a terminal, so it may undo an owner
        # release. The router's POST /lease/restore deliberately does not pass it.
        if args.model:
            r = alloc.owner_restore(args.model)
            for line in r.get("log") or []:
                print(f"  {line}")
            if not r["ok"]:
                print(f"{BAD} {args.model} was not restored ({r['code']})")
                return 1
            print(f"{OK} {args.model} is back")
            return 0
        ids = alloc.restore_now(owner=True)
        print("restored: " + (", ".join(ids) if ids else "nothing is owed"))
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
    # Everything this process records is attributable to a person at a terminal,
    # not to the agent — set once here rather than at each cmd_* handler.
    try:
        from ava_bridge import audit
        audit.set_actor("cli")
    except Exception:  # noqa: BLE001 — `ava --help` must not need the package
        pass
    p = argparse.ArgumentParser(prog="ava", description="Ava control CLI")
    sub = p.add_subparsers(dest="cmd")
    atp = sub.add_parser("attest", help="evidence bundle: what this box can show, "
                                       "and what it cannot")
    atp.add_argument("--json", action="store_true")
    atp.add_argument("--out", help="write the bundle here (the ONLY thing that writes)")
    atp.add_argument("--redact-biometrics", action="store_true",
                     dest="redact_biometrics",
                     help="omit the voiceprint digest — pass this before sharing")
    atp.set_defaults(func=cmd_attest)
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
    clp = sub.add_parser("claim", help="print the first-run claim token and link")
    clp.add_argument("--url", default="", metavar="BASE",
                     help="base URL to build the link from (default http://localhost:8096)")
    clp.add_argument("--no-browser", action="store_true",
                     help="print the link but do not open it")
    clp.set_defaults(func=cmd_claim)
    ap = sub.add_parser("agent", help="agent runtime (NemoClaw): status / provision")
    ap.add_argument("action", nargs="?",
                    choices=["status", "provision", "adopt-state", "prune"],
                    default="status")
    ap.add_argument("--install", action="store_true",
                    help="auto `npm install -g nemoclaw` if the CLI is missing")
    # Both of these move or delete files, so both report and stop unless told
    # otherwise. `adopt-state` in particular reaches OUTSIDE this AVA_HOME.
    ap.add_argument("--write", action="store_true",
                    help="adopt-state / prune: actually do it (default: report only)")
    ap.add_argument("--only", default="all",
                    metavar="SCOPE",
                    help="deploy just part of the kit: persona, policies, "
                         "servers, skills (comma-separated), or all (default)")
    ap.set_defaults(func=cmd_agent)
    cp = sub.add_parser("connector", help="list / scaffold / generate policies+tools for connectors")
    cp.add_argument("action", choices=["list", "apps", "new", "policies", "tools"])
    cp.add_argument("name", nargs="?", help="connector name (for new / policies / tools)")
    # The two paths hang off the AGENT STATE root, which is AVA_HOME-based: these
    # files are rendered from a manifest, not authored, and the install root is
    # an image layer under Docker — writing them there meant every rebuild threw
    # away the tools and policies while keeping the manifests that produced them.
    # The two roots are the same directory on a bare-metal install, which is why
    # anchoring on the install root looked correct for so long. Printed in full
    # rather than relatively, so the answer is unambiguous on both shapes.
    cp.add_argument("--write", action="store_true",
                    help=f"write generated files under the agent state root "
                         f"({settings.agent_state_dir()}): policies -> "
                         f"policies/generated, tools -> "
                         f"mcp_server_connectors/apps")
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
                                     "release / restore / reset / resume")
    ap.add_argument("action", choices=["status", "plan", "release", "restore",
                                       "reset", "resume"])
    ap.add_argument("model", nargs="?",
                    help="plan/release/restore/reset: the declared model id")
    ap.add_argument("--mode", choices=["unload", "stop"],
                    help="release: which lever to pull (default: the cheapest offered)")
    ap.set_defaults(func=cmd_alloc)

    mp = sub.add_parser("models", help="model store: list / pull / verify / bench")
    mp.add_argument("action", choices=["list", "pull", "verify", "bench"])
    mp.add_argument("name", nargs="?",
                    help="pull: role (chat/fast); bench: the prompt")
    mp.add_argument("--auto", action="store_true",
                    help="pull the chat/fast model that fits the detected memory tier")
    mp.add_argument("--models", help="bench: comma-separated backend ids/models to compare")
    mp.add_argument("--max-tokens", type=int, default=200, dest="max_tokens",
                    help="bench: completion length per model (default 200)")
    mp.set_defaults(func=cmd_models)

    args = p.parse_args()
    if not getattr(args, "func", None):
        p.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
