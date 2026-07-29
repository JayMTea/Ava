#!/usr/bin/env python3
"""Ava on-device validation — RUN THIS ON THE REAL MACHINE, whatever it is.

Read-only. Nothing is written, downloaded or started unless you pass --record.

This is the tool that PROMOTES a row in deploy/platforms.conf. A tier above
`ci-simulated` has to cite evidence that exists, and this produces it:

    python3 tools/ondevice_check.py --record

writes docs/evidence/<platform-key>-<date>.json, which
tests/test_platform_matrix_ssot.py then requires to exist and to agree with the
row it backs. That is the whole anti-marketing mechanism: you cannot claim
"verified on device" without a committed artifact recorded on that device.

It generalises tools/mac_ondevice_check.py, which did this for exactly one
platform. The checks are the same shape — does the HAL detect this machine, does
model-fit recommend a tier a locally-servable engine can actually serve, is a
local engine reachable — asked of whatever hardware is present.

    python3 tools/ondevice_check.py
    python3 tools/ondevice_check.py --json
    python3 tools/ondevice_check.py --record
    python3 tools/ondevice_check.py --engine-url http://127.0.0.1:11434/v1 --smoke

Exit 0 = this machine looks correctly detected; 1 = a hard problem a user would
hit; warnings never fail the run (they flag an unconfigured optional capability,
not a broken claim — the same rule `ava verify` uses).
"""
from __future__ import annotations

import argparse
import json
import os
import platform as _platform
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

OK, WARN, BAD = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m"
_hard: list[str] = []
_warn: list[str] = []
_quiet = False


def ok(msg: str) -> None:
    if not _quiet:
        print(f"  {OK} {msg}")


def warn(msg: str) -> None:
    _warn.append(msg)
    if not _quiet:
        print(f"  {WARN} {msg}")


def bad(msg: str) -> None:
    _hard.append(msg)
    if not _quiet:
        print(f"  {BAD} {msg}")


def head(t: str) -> None:
    if not _quiet:
        print(f"\n\033[1m{t}\033[0m")


def _sh(cmd: list[str], timeout: int = 4) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # absent tool is a normal answer here
        return ""


def _raw_readings() -> dict:
    """The unparsed bytes each reader saw, for the recorded fixture.

    Recording the RAW strings rather than only the parsed result is the point: a
    contributor with no hardware of this class can replay these through the real
    providers and get a red test when they break the parsing. Parsed-only
    evidence proves the numbers were once right and protects nothing.
    """
    from ava_bridge import hwinfo
    raw: dict = {
        "nvidia_smi_query": _sh(
            ["nvidia-smi", "--query-gpu=name,memory.total,memory.free,power.draw",
             "--format=csv,noheader"]),
        "rocm_smi_json": _sh(["rocm-smi", "--showmeminfo", "vram", "--json"]),
        "xpu_smi_stats": _sh(["xpu-smi", "stats", "-j"]),
        "sysctl_brand": _sh(["sysctl", "-n", "machdep.cpu.brand_string"]),
        "drm_cards": hwinfo._drm_cards(),
        "drm_sysfs": {},
    }
    for path, vendor in hwinfo._drm_cards():
        dev = os.path.join(path, "device")
        entry: dict = {"vendor": vendor}
        for leaf in ("mem_info_vram_total", "mem_info_vram_used",
                     "mem_info_gtt_total", "gpu_busy_percent", "product_name"):
            entry[leaf] = hwinfo._read_sysfs(os.path.join(dev, leaf))
        hw = os.path.join(dev, "hwmon")
        try:
            for n in sorted(os.listdir(hw)):
                for leaf in ("power1_average", "temp1_input"):
                    v = hwinfo._read_sysfs(os.path.join(hw, n, leaf))
                    if v is not None:
                        entry[f"hwmon/{leaf}"] = v
        except OSError:
            pass
        raw["drm_sysfs"][os.path.basename(path)] = entry
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            raw["meminfo_total_kb"] = next(
                (ln.split()[1] for ln in f if ln.startswith("MemTotal")), None)
    except OSError:
        raw["meminfo_total_kb"] = None
    return raw


def _probe(url: str, timeout: float = 1.5) -> bool:
    try:
        import requests
        return requests.get(url, timeout=timeout).status_code < 500
    except Exception:  # unreachable engine is a normal answer here
        return False


def main() -> int:
    global _quiet
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true",
                    help="emit the report as JSON to stdout and nothing else")
    ap.add_argument("--record", action="store_true",
                    help="write docs/evidence/<key>-<date>.json (the ONLY write)")
    ap.add_argument("--engine-url", default="",
                    help="OpenAI-compatible base URL of your local engine")
    ap.add_argument("--smoke", action="store_true",
                    help="send one tiny /chat/completions if a model is up")
    ap.add_argument("--model", default="", help="model id for --smoke")
    args = ap.parse_args()
    _quiet = args.json

    from ava_bridge import hwinfo, model_fit, models, platforms

    head("Platform")
    pid = hwinfo.platform_id()
    row = platforms.detect()
    if row is None:
        bad(f"detected {pid!r}, which has no row in deploy/platforms.conf — "
            "add one before recording evidence")
    else:
        ok(f"{pid} -> {row.key}  [{row.tier}]")
        if row.tier == "unsupported":
            warn(f"{row.label} is marked unsupported; readings below are FYI only")

    model, why = hwinfo._memory_model()
    ok(f"memory model: {model} ({why})")
    if model == "unknown":
        # Not a defect by itself: `_memory_model` only has an explicit signal on
        # AMD, and Grace-class NVIDIA is correctly resolved by the VRAM/RAM size
        # ratio instead. It is only worth flagging when the row disagrees, which
        # is checked under Memory below — so state the mechanism, don't cry wolf.
        expect = row.mem_model if row else "?"
        ok(f"no APU-specific signal (normal off AMD); the size heuristic decides, "
           f"and the matrix expects {expect}. Override with "
           f"AVA_GPU_MEMORY_MODEL=unified|discrete if that is wrong here.")

    head("Memory")
    fit, sysm, vram = hwinfo.fit_memory(), hwinfo.system_mem(), hwinfo.vram_mem()
    if not fit.readable:
        bad("no readable memory pool — model-fit gating is disabled on this box")
    else:
        ok(f"fit pool: {fit.free_gb:.1f}/{fit.total_gb:.1f} GB free "
           f"(source {fit.source})")
        if fit.free_gb > fit.total_gb + 1e-6:
            bad("free exceeds total — the reading cannot be true")
    if sysm.readable:
        ok(f"system: {sysm.free_gb:.1f}/{sysm.total_gb:.1f} GB ({sysm.source})")
    if vram.readable:
        ok(f"vram:   {vram.free_gb:.1f}/{vram.total_gb:.1f} GB ({vram.source})")
    else:
        ok("vram:   not reported (expected on unified memory and on CPU-only)")
    if row and row.mem_model in ("unified", "system") and vram.readable \
            and fit.source and fit.source.startswith("vram"):
        bad(f"row says {row.mem_model} but fit gated on {fit.source} — "
            "the matrix and the fit path disagree about this machine")

    head("GPU telemetry")
    gl = hwinfo.gpus()
    if not gl:
        ok("no accelerator reported (correct on CPU-only)")
    for g in gl:
        bits = [f"util {g.util}%" if g.util is not None else "util —",
                f"{g.temp_c}°C" if g.temp_c is not None else "temp —",
                f"{g.power_w} W" if g.power_w is not None else "power —"]
        ok(f"{g.name} [{g.source}] " + " · ".join(bits))
    powered = any(g.power_w is not None for g in gl)
    if row and row.power_measurable and not powered:
        warn(f"row claims power via {row.power_source} but nothing reported it — "
             "energy figures on this box will be a labelled estimate")
    if powered:
        ok("GPU power is readable, so energy here is measured rather than nominal")

    head("Model fit and engine")
    tier, avail = models.detected_tier()
    ok(f"recommended tier: {tier}" + (f" ({avail:.0f} GB available)"
                                      if avail else ""))
    if row:
        servable = models.engine_servable_here(row.engine)
        (ok if servable else bad)(
            f"row's engine {row.engine!r} servable here: {servable}")
        if not servable:
            bad("the matrix recommends an engine this platform cannot serve — "
                "pulling weights for it would be the trap engine_servable_here "
                "exists to prevent")
    try:
        from ava_bridge import config as _cfg
        backends = list(getattr(_cfg, "BACKENDS", []) or [])
        if backends:
            rep = model_fit.fit_report(backends)
            fits = sum(1 for r in rep.get("rows", []) if r.get("fits"))
            ok(f"fit report: {fits}/{len(backends)} declared backend(s) fit now")
        else:
            ok("no backends declared yet (fresh install) — nothing to fit-check")
    except Exception as e:  # diagnostics must never be fatal
        warn(f"fit_report unavailable: {e}")

    if args.engine_url:
        head("Local engine")
        base = args.engine_url.rstrip("/")
        if _probe(f"{base}/models"):
            ok(f"{base}/models answered")
            if args.smoke:
                warn("--smoke not implemented for the generic checker yet; "
                     "use `ava verify` for an end-to-end turn")
        else:
            warn(f"{base}/models did not answer — start your engine, or omit "
                 "--engine-url")

    report = {
        "schema": "ava-ondevice/1",
        "platform_key": row.key if row else None,
        "recorded_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "host": {"machine": _platform.machine(), "system": _platform.system(),
                 "release": _platform.release(),
                 "python": _platform.python_version()},
        "repo_head": _sh(["git", "rev-parse", "--short", "HEAD"]),
        "repo_dirty_files": len([ln for ln in _sh(
            ["git", "status", "--porcelain"]).splitlines() if ln]),
        "hwinfo": hwinfo.snapshot(),
        "raw": _raw_readings(),
        "checks": {"hard_failures": _hard, "warnings": _warn},
        "notes": [],
    }

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        head("Result")
        if _hard:
            print(f"  {BAD} {len(_hard)} hard problem(s); {len(_warn)} warning(s)")
        else:
            print(f"  {OK} no hard problems; {len(_warn)} warning(s)")

    if args.record:
        if not report["platform_key"]:
            print("refusing to record: this machine matches no row, so the "
                  "report could not be joined to a claim", file=sys.stderr)
            return 1
        if _hard:
            print("refusing to record: there are hard failures, and evidence "
                  "recorded from a machine that fails its own checks is worse "
                  "than no evidence", file=sys.stderr)
            return 1
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "docs", "evidence")
        os.makedirs(d, exist_ok=True)
        day = time.strftime("%Y-%m-%d", time.gmtime())
        path = os.path.join(d, f"{report['platform_key']}-{day}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(report, f, indent=2, default=str)
            f.write("\n")
        rel = os.path.relpath(path, root)
        print(f"\nrecorded {rel}\n"
              f"  point that row's `evidence` column at it in "
              f"deploy/platforms.conf, raise its `tier`, then run\n"
              f"  python3 -m ava_bridge.platforms --sync   # re-renders both docs "
              f"tables\n"
              f"  python -m pytest tests/test_platform_matrix_ssot.py -q")

    return 1 if _hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
