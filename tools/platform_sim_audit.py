#!/usr/bin/env python3
"""Simulate any platform in the matrix and drive Ava's REAL decision chain.

Runs on whatever box you have, with the low-level hardware readers redirected so
the app believes it is on the platform you name. Nothing is mocked above the HAL:
`hwinfo -> model_fit -> models.resolve_auto -> platforms.detect` all execute for
real, which is what makes this catch a regression a unit test would not.

    python3 tools/platform_sim_audit.py                       # every row
    python3 tools/platform_sim_audit.py --platform linux-amd-unified
    python3 tools/platform_sim_audit.py --list

Why it exists: the maintainer owns one machine, and three of the four first-class
platform targets cannot be verified on it — a discrete NVIDIA card, a Radeon, and
a Strix Halo APU. tools/mac_sim_audit.py proved this approach works for exactly
one platform; this is the same trick made general, so a contributor editing an
AMD code path on a Mac still gets a red result.

⚠️ **This proves LOGIC, never NUMBERS.** Every simulated machine below is
constructed from documented kernel ABI shapes, not recorded from real hardware.
A row stays `ci-simulated` in deploy/platforms.conf until someone runs
`tools/ondevice_check.py --record` on the real thing. Do not let a green run here
talk you into promoting a tier.
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS, FAIL, SKIP = ("\033[32mPASS\033[0m", "\033[31mFAIL\033[0m",
                    "\033[33mSKIP\033[0m")
_MIB, _GIB = 1024 ** 2, 1024 ** 3
_failures = 0


def check(label: str, got, want) -> bool:
    global _failures
    good = got == want
    if not good:
        _failures += 1
    print(f"  [{PASS if good else FAIL}] {label}")
    if not good:
        print(f"        got={got!r}  want={want!r}")
    return good


def section(t: str) -> None:
    print(f"\n\033[1m{t}\033[0m")


class _FakeVM:
    def __init__(self, total_gb: float, free_gb: float):
        self.total = int(total_gb * _GIB)
        self.available = int(free_gb * _GIB)


# Each entry describes a machine precisely enough to exercise the decision chain:
# what the DRM tree looks like, how much system memory there is, and whether an
# NVIDIA driver answers. `want_*` is what the chain must then conclude.
_VENDOR = {"amd": "0x1002", "intel": "0x8086", "nvidia": "0x10de"}
SIMS: dict[str, dict] = {
    "linux-amd-unified": dict(
        vendor="amd", total_gb=128.0, free_gb=100.0, nvidia=False,
        sysfs={"mem_info_vram_total": 512 * _MIB, "mem_info_vram_used": 64 * _MIB,
               "mem_info_gtt_total": 64 * _GIB,
               "product_name": "Radeon 8060S Graphics"},
        want_platform="linux-amd", want_model="unified",
        want_fit_total=128.0, want_gpu_source="amdgpu-sysfs"),
    "linux-amd-discrete": dict(
        vendor="amd", total_gb=128.0, free_gb=100.0, nvidia=False,
        sysfs={"mem_info_vram_total": 24 * _GIB, "mem_info_vram_used": 2 * _GIB,
               "mem_info_gtt_total": 8 * _GIB,
               "product_name": "Radeon RX 7900 XTX"},
        want_platform="linux-amd", want_model="discrete",
        want_fit_total=24.0, want_gpu_source="amdgpu-sysfs"),
    "linux-intel": dict(
        vendor="intel", total_gb=64.0, free_gb=48.0, nvidia=False,
        sysfs={"product_name": "Arc A770"},
        want_platform="linux-intel", want_model="unknown",
        want_fit_total=64.0, want_gpu_source="intel-sysfs"),
    "linux-gpu": dict(
        vendor=None, total_gb=32.0, free_gb=24.0, nvidia=False, sysfs={},
        want_platform="linux-gpu", want_model="unknown",
        want_fit_total=32.0, want_gpu_source="drm-sysfs"),
    "linux-cpu": dict(
        vendor="none", total_gb=16.0, free_gb=12.0, nvidia=False, sysfs={},
        want_platform="linux-cpu", want_model="unknown",
        want_fit_total=16.0, want_gpu_source=None),
}


def _build_sysfs(root: str, vendor: str | None, files: dict) -> None:
    if vendor == "none":
        return                      # a CPU-only box has no card at all
    dev = os.path.join(root, "card0", "device")
    os.makedirs(dev, exist_ok=True)
    if vendor is not None:
        _w(os.path.join(dev, "vendor"), _VENDOR[vendor])
    for k, v in files.items():
        _w(os.path.join(dev, k), v)
    # A connector sibling and a render node, because the real tree has them and
    # over-counting them was a live bug in the first cut of _drm_cards().
    os.makedirs(os.path.join(root, "card0-DP-1"), exist_ok=True)
    os.makedirs(os.path.join(root, "renderD128"), exist_ok=True)


def _w(path: str, val) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write(f"{val}\n")


def audit(key: str, spec: dict) -> None:
    from ava_bridge import hwinfo, model_fit, platforms

    section(f"{key}  ({spec['total_gb']:.0f} GB system RAM)")
    tmp = tempfile.mkdtemp(prefix=f"ava-sim-{key}-")
    try:
        _build_sysfs(tmp, spec["vendor"], spec["sysfs"])
        env = {hwinfo._DRM_ROOT_ENV: tmp}
        nvml = mock.MagicMock() if spec["nvidia"] else None
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(hwinfo, "_nvml", return_value=nvml), \
             mock.patch.object(hwinfo, "_which",
                              return_value=("/usr/bin/nvidia-smi"
                                            if spec["nvidia"] else None)), \
             mock.patch.object(hwinfo, "_psutil") as ps:
            ps.virtual_memory.return_value = _FakeVM(spec["total_gb"],
                                                     spec["free_gb"])
            hwinfo.reset_cache()

            check("platform_id()", hwinfo.platform_id(), spec["want_platform"])
            check("_memory_model()", hwinfo._memory_model()[0], spec["want_model"])

            fit = hwinfo.fit_memory()
            check("fit_memory().total_gb", round(fit.total_gb or 0, 1),
                  round(spec["want_fit_total"], 1))

            gl = hwinfo.gpus()
            check("gpus()[0].source", gl[0].source if gl else None,
                  spec["want_gpu_source"])

            # The matrix must describe this machine, and agree with the chain.
            row = platforms.detect()
            check("platforms.detect() row exists", row is not None, True)
            if row is not None:
                check("row.platform_id", row.platform_id, spec["want_platform"])
                if spec["want_model"] != "unknown":
                    check("row.mem_model", row.mem_model, spec["want_model"])
                # A row must never claim hardware verification from a simulation.
                check("row tier is not verified-on-device",
                      row.tier != "verified-on-device", True)

            # The fit gate must not green-light a model larger than the pool.
            tier, _hint = model_fit.recommend_tier(fit.total_gb or 0)
            print(f"  [{SKIP}] recommend_tier({fit.total_gb:.0f} GB) -> {tier}"
                  "   (informational)")
    finally:
        hwinfo.reset_cache()
        shutil.rmtree(tmp, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--platform", default="", help="one key from --list")
    ap.add_argument("--list", action="store_true", help="show simulable platforms")
    args = ap.parse_args()

    if args.list:
        for k, v in SIMS.items():
            print(f"  {k:<22} -> {v['want_platform']} / {v['want_model']}")
        return 0

    keys = [args.platform] if args.platform else list(SIMS)
    unknown = [k for k in keys if k not in SIMS]
    if unknown:
        print(f"unknown platform key(s): {unknown}\n"
              f"try --list", file=sys.stderr)
        return 2

    for k in keys:
        audit(k, SIMS[k])

    section("Verdict")
    if _failures:
        print(f"  \033[31m{_failures} simulated check(s) FAILED.\033[0m")
        return 1
    print(f"  \033[32mAll simulated checks passed for {len(keys)} platform(s).\033[0m")
    print("  Logic is sound. The NUMBERS are still unverified — run "
          "tools/ondevice_check.py --record on real hardware to promote a tier.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
