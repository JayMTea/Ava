# Hardware abstraction layer — on-device validation

`ava_bridge/hwinfo.py` is the single place the app reads hardware (memory, GPU),
consumed by the model-fit router (`model_fit.py` → `ava_router.py /fit`) and the
dashboard monitor (`hardware.py`). Its decision logic is unit-tested per platform
by simulation in `tests/test_hwinfo.py`, but the **numbers** on non-Linux hardware
can only be trusted after running it on the real device. This is that checklist.

## Platform support matrix

| Platform | Fit memory source | GPU util/temp/power | GPU/unified memory | Status |
|---|---|---|---|---|
| Linux + discrete NVIDIA | free **VRAM** (NVML→nvidia-smi) | ✅ NVML/smi | ✅ VRAM | verified logic; needs a discrete box to confirm VRAM path |
| DGX Spark GB10 (unified) | system RAM (psutil) | ✅ nvidia-smi | unified via system RAM | **verified on-device** |
| Apple Silicon Mac | system RAM (psutil) | ❌ None (no unprivileged API) | ✅ unified via system RAM | **needs on-device validation** |
| CPU-only / other | system RAM (psutil→/proc) | ❌ None | system RAM | verified logic |
| No psutil, non-Linux | none → gating disabled | ❌ None | none | verified logic (degrades safely) |

## Run this on the Mac mini

```bash
pip install -r requirements.txt          # pulls psutil; nvidia-ml-py stays inactive
python3 -c "from ava_bridge import hwinfo, json; print(__import__('json').dumps(hwinfo.snapshot(), indent=2))"
```

**Expect:**
- `platform` == `"darwin-apple"`.
- `fit_memory.source` == `"system-psutil"`, `total_gb` ≈ the Mac's installed RAM
  (16 / 24 / 32 / 64 / 128), `free_gb` a plausible live figure.
- `system_memory` matches `fit_memory` (unified memory → same pool).
- `gpus[0].name` contains the SoC (e.g. "Apple M4 Pro GPU");
  `util`/`temp_c`/`power_w` are `null` (**expected** — see caveat); `mem_total_gb`
  equals installed RAM.
- `have.psutil` == true, `have.nvml` == false.

**Then confirm the fit router:**
```bash
# with two Ollama models configured per config.example.yaml's Apple example:
curl -s -H "X-Ava-Router-Token: $AVA_ROUTER_TOKEN" localhost:8010/fit | python3 -m json.tool
```
- `gating` == `"enabled"` (NOT "disabled" — that would mean memory couldn't be read).
- `mem_source` == `"system-psutil"`, `platform` == `"darwin-apple"`.
- Each backend shows `local: true` and a `serve_reason` referencing the live figure.
- Load a large model + start something memory-heavy, re-hit `/fit`: the big
  backend should flip to `serve_ok: false` ("SHED") while the small one stays true.

## Known caveat (by design, not a bug)

Apple GPU **utilisation / temperature / power** have no unprivileged API:
`powermetrics` needs `sudo`, and Metal performance counters are private. The HAL
returns `null` for these rather than fabricating numbers, so the Mac dashboard
shows memory + CPU reliably and dashes for GPU util/temp/power. Wiring
`sudo powermetrics --samplers gpu_power` (or a Metal helper) is a possible future
provider in `hwinfo._apple_gpus()` — deliberately out of scope here.

## Adding a new accelerator later

Write one provider in `hwinfo.py` (a `_xxx_gpus()` returning `list[GpuInfo]` and,
if it has dedicated memory, a branch in `vram_mem()`), then add its class to
`platform_id()` / `gpus()`. Nothing in `model_fit.py`, `ava_router.py`, or
`hardware.py` changes — that is the point of the HAL.
