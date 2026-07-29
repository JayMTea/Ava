# Hardware abstraction layer: on-device validation

`ava_bridge/hwinfo.py` is the single place the app reads hardware (memory, GPU),
consumed by the model-fit router (`model_fit.py` → `ava_router.py /fit`) and the
dashboard monitor (`hardware.py`). Its decision logic is unit-tested per platform
by simulation in `tests/test_hwinfo.py`, but the **numbers** on non-Linux hardware
can only be trusted after running it on the real device. This is that checklist.

## Platform support matrix

**Do not edit this table by hand.** It renders from `deploy/platforms.conf` via
`python3 -m ava_bridge.platforms --sync`, and `tests/test_platform_matrix_ssot.py`
fails if it drifts. It used to be maintained by hand alongside a second table in
`deploy/README.md`, and the two had already come to disagree about Apple Silicon.

<!-- platforms:begin:hwinfo — generated from deploy/platforms.conf -->
| Platform | Fit memory source | GPU power | Tier | Evidence |
|---|---|---|---|---|
| Unified-memory NVIDIA (GB10 / Grace-Blackwell) | system RAM (unified) | Yes (NVML / nvidia-smi) | verified-on-device | `docs/evidence/linux-nvidia-unified-2026-07-29.json` |
| Linux + discrete NVIDIA (RTX / data-centre) | free VRAM | Yes (NVML / nvidia-smi) | ci-simulated | — |
| AMD APU (Strix Halo / Ryzen AI Max) | system RAM (unified) | Yes (amdgpu hwmon) | ci-simulated | — |
| AMD discrete (Radeon / ROCm) | free VRAM | Yes (amdgpu hwmon) | ci-simulated | — |
| Intel Arc / Xe | free VRAM | Yes (xpu-smi) | ci-simulated | — |
| Linux, GPU present but unidentifiable | system RAM (unverified) | None | ci-simulated | — |
| CPU-only Linux | system RAM | CPU package only (x86 RAPL) | ci-native | `ci.yml:test` |
| Apple Silicon (Mac mini / Studio / laptop) | system RAM (unified) | None | ci-simulated | — |
| Intel Mac | free VRAM | None | unsupported | — |
| Windows + NVIDIA | free VRAM | Yes (NVML / nvidia-smi) | ci-simulated | — |
| Windows, no NVIDIA | system RAM | None | ci-simulated | — |
| Unrecognised platform (gating disabled) | system RAM (unverified) | None | ci-simulated | — |
<!-- platforms:end -->

**Reading the Tier column.** `verified-on-device` means a human ran
`tools/ondevice_check.py` on real hardware of that class and committed the report
named in Evidence. `ci-native` means a CI job exercises the real code on real
hardware of that class. `ci-simulated` means the decision logic is tested against
constructed or recorded sysfs bytes — **the parsing is tested, the numbers are
not**. `community-reported` is someone else's on-device report.
`unsupported` is detected and refused.

Two honest consequences of that vocabulary, as of this writing: the AMD rows are
`ci-simulated` against **constructed** fixtures, because the maintainer owns no
AMD hardware and Strix Halo cannot be rented — see the warning at the top of
`tests/test_hwinfo_amd.py`. And Apple Silicon stays `ci-simulated` until either
someone runs the on-device check or the repo goes public and a `macos-14` CI
runner becomes available.

## Run this on an Apple Silicon Mac

```bash
pip install -r requirements.txt          # pulls psutil; nvidia-ml-py stays inactive
python3 -c "import json; from ava_bridge import hwinfo; print(json.dumps(hwinfo.snapshot(), indent=2))"
```

**Expect:**

- `platform` == `"darwin-apple"`.
- `fit_memory.source` == `"system-psutil"`, `total_gb` ≈ the Mac's installed RAM
  (16 / 24 / 32 / 64 / 128), `free_gb` a plausible live figure.
- `system_memory` matches `fit_memory` (unified memory means the same pool).
- `gpus[0].name` contains the SoC (e.g. "Apple M4 Pro GPU");
  `util`/`temp_c`/`power_w` are `null` (**expected**; see the caveat below);
  `mem_total_gb` equals installed RAM.
- `have.psutil` == true, `have.nvml` == false.

**Then confirm the fit router:**

```bash
# with two Ollama models configured per config.example.yaml's Apple example:
curl -s -H "X-Ava-Router-Token: $AVA_ROUTER_TOKEN" localhost:8010/fit | python3 -m json.tool
```

- `gating` == `"enabled"` (NOT "disabled"; that would mean memory could not be read).
- `mem_source` == `"system-psutil"`, `platform` == `"darwin-apple"`.
- Each backend shows `local: true` and a `serve_reason` referencing the live figure.
- Load a large model, start something memory-heavy, and re-hit `/fit`: the big
  backend should flip to `serve_ok: false` ("SHED") while the small one stays true.

## Known caveat (by design, not a bug)

Apple GPU **utilization, temperature, and power** have no unprivileged API:
`powermetrics` needs `sudo`, and Metal performance counters are private. The HAL
returns `null` for these rather than fabricating numbers, so the Mac dashboard
shows memory and CPU reliably, and dashes for GPU util/temp/power. Wiring
`sudo powermetrics --samplers gpu_power` (or a Metal helper) is a possible future
provider in `hwinfo._apple_gpus()`, deliberately out of scope here.

## Adding a new accelerator later

Write one provider in `hwinfo.py` (a `_xxx_gpus()` returning `list[GpuInfo]` and,
if it has dedicated memory, a branch in `vram_mem()`), then add its class to
`platform_id()` / `gpus()`. Nothing in `model_fit.py`, `ava_router.py`, or
`hardware.py` changes; that is the point of the HAL.
