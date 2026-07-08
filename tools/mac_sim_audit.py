#!/usr/bin/env python3
"""Mac Studio (M3 Ultra / 256GB) first-run SIMULATION + audit.

Runs on THIS Linux box but drives Ava's *real* code paths with the low-level
hardware readers monkeypatched to look exactly like an Apple-Silicon Mac Studio
with 256 GiB unified memory and no NVIDIA GPU. Proves the decision logic (platform
detection, unified-memory fit, model routing, wizard tiering) behaves correctly
BEFORE we get SSH to the real machine. The absolute byte-for-byte numbers still
need on-device validation — that's what the companion harness (mac_ondevice_check.py)
is for.

Usage:  python mac_sim_audit.py
Exit 0 = all simulated checks pass; non-zero = something a Mac user would hit.
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

# Run from anywhere: put the repo root (this file's grandparent) on the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ava_bridge import hwinfo, model_fit  # noqa: E402
from ava_bridge import setup_wizard  # noqa: E402

# ── the simulated machine ────────────────────────────────────────────────────
TOTAL_GB = 256.0          # M3 Ultra Mac Studio, 256 GB unified memory
FREE_GB_IDLE = 210.0      # fresh boot, OS + Ava using ~46 GB
G = 1024 ** 3
SOC = "Apple M3 Ultra"

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
_failures = 0


def check(label: str, got, want, ok=None):
    global _failures
    good = ok if ok is not None else (got == want)
    if not good:
        _failures += 1
    print(f"  [{PASS if good else FAIL}] {label}")
    print(f"        got={got!r}  want={want!r}")
    return good


class _FakePsutil:
    """Stands in for psutil.virtual_memory() on a 256 GB Mac (free varies)."""
    def __init__(self, free_gb):
        self._free = free_gb

    def virtual_memory(self):
        return SimpleNamespace(total=int(TOTAL_GB * G),
                               available=int(self._free * G))


def _reset():
    hwinfo._platform_id = None
    hwinfo._fit_cache.update(ts=0.0, info=None)


def mac_env(free_gb=FREE_GB_IDLE):
    """Context stack that makes hwinfo believe it's on this Mac Studio."""
    fake_sys = SimpleNamespace(platform="darwin")
    fake_platform = mock.Mock()
    fake_platform.machine.return_value = "arm64"
    return [
        mock.patch.object(hwinfo, "sys", fake_sys),
        mock.patch.object(hwinfo, "_platform", fake_platform),
        mock.patch.object(hwinfo, "_nvml", return_value=None),        # no NVIDIA
        mock.patch.object(hwinfo, "_which", return_value=None),       # no nvidia-smi
        mock.patch.object(hwinfo, "_psutil", _FakePsutil(free_gb)),   # unified RAM
        # sysctl brand string on Apple Silicon
        mock.patch.object(hwinfo, "_run", return_value=SOC),
    ]


def _enter(patches):
    for p in patches:
        p.start()


def _exit(patches):
    for p in patches:
        p.stop()


# realistic Mac backend set (from config.example.yaml's Apple Silicon example,
# with the Omni brain served via Ollama's OpenAI-compatible API on :11434)
MAC_BACKENDS = [
    {"id": "local-omni", "engine": "ollama",
     "url": "http://127.0.0.1:11434/v1",
     "model": "nemotron-open-model-30b",
     "fit": {"weight_gb": 35, "tier": "large", "min_free_gb": 10,
             "workloads": ["chat", "reasoning", "code", "vision", "audio"]}},
    {"id": "mac-fast", "engine": "ollama",
     "url": "http://127.0.0.1:11434/v1",
     "model": "llama3.1:8b",
     "fit": {"weight_gb": 6, "tier": "small", "min_free_gb": 3,
             "workloads": ["fast", "cheap", "utility", "chat"]}},
]


def section(title):
    print(f"\n\033[1m{title}\033[0m")


def main():
    print(f"=== Ava Mac Studio simulation — {SOC}, {TOTAL_GB:.0f} GB unified ===")

    # 1) Hardware abstraction layer -------------------------------------------
    section("1. Platform + memory detection (hwinfo)")
    _reset()
    patches = mac_env()
    _enter(patches)
    try:
        pid = hwinfo.platform_id()
        check("platform_id() classifies as Apple Silicon", pid, "darwin-apple")

        sysm = hwinfo.system_mem()
        check("system_mem source is psutil", sysm.source, "system-psutil")
        check("system_mem total ~= 256 GB", round(sysm.total_gb), 256)

        vram = hwinfo.vram_mem()
        check("vram_mem is empty (no discrete GPU)", vram.readable, False, ok=(not vram.readable))

        fm = hwinfo.fit_memory()
        check("fit_memory uses the unified system pool", fm.source, "system-psutil")
        check("fit_memory is readable (gating enabled)", fm.readable, True, ok=fm.readable)
        check("fit_memory free ~= 210 GB", round(fm.free_gb), 210)

        g = hwinfo.gpu()
        check("Apple GPU name carries the SoC brand", SOC in (g.name or ""),
              True, ok=(SOC in (g.name or "")))
        check("Apple GPU util is honestly None (no unprivileged API)",
              g.util, None, ok=g.util is None)
        check("Apple GPU reports unified mem_total ~= 256",
              round(g.mem_total_gb or 0), 256)

        snap = hwinfo.snapshot()
        check("snapshot platform", snap["platform"], "darwin-apple")
        check("snapshot gating source", snap["fit_memory"]["source"], "system-psutil")
    finally:
        _exit(patches)

    # 2) Model-fit routing ----------------------------------------------------
    section("2. Model-fit routing on unified memory (model_fit)")
    _reset()
    patches = mac_env()
    _enter(patches)
    try:
        check("free_mem_gb resolves from unified pool", round(model_fit.free_mem_gb()), 210)
        check("mem_source is a system-* label", model_fit.mem_source(), "system-psutil")

        # chat should land on the big Omni brain
        chat = model_fit.select(MAC_BACKENDS, workload="chat")
        top = chat[0]
        check("chat routes to the Omni brain", top.backend["id"], "local-omni")
        check("  ...and it fits in unified memory", top.fits, True, ok=top.fits)

        # a fast/utility turn should prefer the small model
        fast = model_fit.select(MAC_BACKENDS, workload="fast")
        check("fast/utility routes to the small model", fast[0].backend["id"], "mac-fast")
    finally:
        _exit(patches)

    # 3) Memory pressure sheds to the smaller model ---------------------------
    section("3. Memory-pressure shedding (concurrent render eats the pool)")
    _reset()
    patches = mac_env(free_gb=8.0)     # heavy the GPU service render: only 8 GB free
    _enter(patches)
    try:
        chat = model_fit.select(MAC_BACKENDS, workload="chat", free_gb=8.0)
        omni = next(c for c in chat if c.backend["id"] == "local-omni")
        small = next(c for c in chat if c.backend["id"] == "mac-fast")
        check("under pressure the Omni brain is SHED (won't OOM the box)",
              omni.fits, False, ok=(not omni.fits))
        check("the small model still fits", small.fits, True, ok=small.fits)
        check("router prefers the model that fits", chat[0].backend["id"], "mac-fast")
    finally:
        _exit(patches)

    # 4) Wizard hardware probe (what the browser setup screen shows) ----------
    section("4. First-run wizard hardware probe (/api/setup/hardware)")
    _reset()
    patches = mac_env()
    _enter(patches)
    try:
        hw = setup_wizard.api_hardware()
        check("wizard reports ~256 GB fit memory", round(hw["fit_gb"]), 256)
        check("wizard memory source is unified/system", hw["source"], "system-psutil")
        check("wizard recommends the LARGE tier (Omni-class)", hw["tier"], "large")
        check("wizard hint names a 30B-class model",
              "30B" in (hw["hint"] or ""), True, ok=("30B" in (hw["hint"] or "")))
    finally:
        _exit(patches)

    # 5) recommend_tier across the Mac line-up (downshift correctness) --------
    section("5. recommend_tier across Mac configs (downshift / cloud fallback)")
    expectations = [
        (512, "large"),   # M3 Ultra 512 GB
        (256, "large"),   # our target
        (96,  "large"),   # M2/M3 Max 96 GB
        (24,  "medium"),  # M-series Mac mini 24 GB
        (16,  "small"),   # entry Mac mini
        (8,   "tiny"),    # very tight
        (4,   "cloud"),   # too little -> hosted API
    ]
    for gb, want in expectations:
        tier, hint = model_fit.recommend_tier(gb)
        check(f"{gb:>4} GB -> {want}", tier, want)

    # ── verdict ──────────────────────────────────────────────────────────────
    section("Verdict")
    if _failures:
        print(f"  \033[31m{_failures} simulated check(s) FAILED.\033[0m")
        sys.exit(1)
    print("  \033[32mAll simulated Mac Studio checks passed.\033[0m")
    print("  Logic is sound; run mac_ondevice_check.py on the real machine to "
          "confirm the numbers.")
    sys.exit(0)


if __name__ == "__main__":
    main()
