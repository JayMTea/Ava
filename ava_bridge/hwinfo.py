"""Ava hardware abstraction layer (HAL).

The one place the whole app asks "what hardware am I on, and how much of it is
free right now?" Before this, `model_fit.py` and `hardware.py` each shelled out
to `nvidia-smi` and read `/proc` independently — duplicated and Linux/NVIDIA-only.
This module centralises that behind a small, platform-agnostic surface so the fit
router and the dashboard both work on:

    - Linux + discrete NVIDIA GPU   (VRAM is the fit-limiting resource)
    - DGX Spark GB10 / Grace-Blackwell (unified memory; nvidia-smi VRAM = N/A)
    - Apple Silicon Mac mini/Studio (unified memory; no nvidia-smi, no /proc)
    - anything else / no GPU / cloud-only (degrades to system memory, then None)

**Native, not text-scraping.** Each reader prefers the most native API available
and only falls back to shelling out:

    system memory : psutil (Mach host_statistics64 on macOS, sysinfo on Linux,
                    GlobalMemoryStatusEx on Windows) -> /proc/meminfo fallback
    discrete VRAM : NVML via nvidia-ml-py (the C API nvidia-smi itself wraps)
                    -> `nvidia-smi` shell fallback
    GPU telemetry : NVML on NVIDIA; best-effort/None on Apple (util/temp/power
                    have no unprivileged API there — see AppleGpuProvider)

Everything is best-effort: any field that can't be read is None, and callers must
treat None as "unknown — don't gate / don't display", never as zero. Adding a new
accelerator later (AMD ROCm, TPU, cloud) means writing one provider here and
nothing else in the app changes.
"""
from __future__ import annotations

import os
import platform as _platform
import subprocess
import sys
import time
from dataclasses import dataclass

# --- optional native libraries (import-guarded; app runs without them) ------- #
try:
    import psutil as _psutil
except Exception:  # noqa: BLE001 — falls back to /proc/meminfo on Linux
    _psutil = None

_pynvml = None
_pynvml_ready = False


def _nvml():
    """Lazily initialise NVML (nvidia-ml-py) once; return the module or None.

    Only present/useful on machines with NVIDIA drivers. A single init is kept
    for the process lifetime; failure is cached so we don't retry every call.
    """
    global _pynvml, _pynvml_ready
    if _pynvml_ready:
        return _pynvml
    _pynvml_ready = True
    try:
        import pynvml  # provided by the `nvidia-ml-py` package
        pynvml.nvmlInit()
        _pynvml = pynvml
    except Exception:  # noqa: BLE001 — no NVIDIA driver / lib not installed
        _pynvml = None
    return _pynvml


# --- data types -------------------------------------------------------------- #
@dataclass
class MemInfo:
    """A free/total memory reading plus where it came from.

    `source` is one of: 'vram-nvml', 'vram-smi', 'system-psutil', 'system-proc',
    or None (nothing readable). Callers gate on `free_gb` and show `source` so a
    user can see exactly what's being measured on their box.
    """
    free_gb: float | None = None
    total_gb: float | None = None
    source: str | None = None

    @property
    def readable(self) -> bool:
        return self.free_gb is not None


@dataclass
class GpuInfo:
    """A single accelerator's live telemetry (any field may be None)."""
    name: str | None = None
    util: float | None = None            # % 0-100
    temp_c: float | None = None
    power_w: float | None = None
    mem_used_gb: float | None = None
    mem_total_gb: float | None = None
    source: str | None = None            # 'nvml' | 'nvidia-smi' | 'apple' | None


# --- DRM card enumeration (Linux) -------------------------------------------- #
# PCI vendor ids, as `/sys/class/drm/cardN/device/vendor` reports them.
_VENDOR_AMD = "0x1002"
_VENDOR_INTEL = "0x8086"
_VENDOR_NVIDIA = "0x10de"
_DRM_ROOT_DEFAULT = "/sys/class/drm"
# Overridable so a platform can be *simulated*. `_nvml()` and `_which()` are
# already mockable, so simulating a CPU-only box was one `mock.patch` — until
# this scan was added and reached through to the real sysfs, at which point
# tests/test_hwinfo.py's CPU-only case started detecting the maintainer's actual
# GPU. It is also how an AMD provider gets tested with no AMD hardware: point
# this at a fixture tree of recorded sysfs bytes.
_DRM_ROOT_ENV = "AVA_DRM_ROOT"


def _drm_root() -> str:
    return os.environ.get(_DRM_ROOT_ENV) or _DRM_ROOT_DEFAULT


def _drm_cards() -> list[tuple[str, str | None]]:
    """`[(sysfs path, PCI vendor id or None)]` for every real DRM card.

    Only `cardN` is a device. Siblings like `card0-Unknown-1` are *connectors*
    (this box has one) and `renderD128` is a render node, so a bare `card*` glob
    over-counts — hence the digits-only check rather than a prefix match.

    A card whose `device/vendor` is unreadable yields `None` rather than being
    dropped: it is still a GPU the box has, and the name-only floor in `gpus()`
    should report it instead of pretending the machine has no accelerator. That
    is the real state of `card0` on the maintainer's own hardware.
    """
    root = _drm_root()
    out: list[tuple[str, str | None]] = []
    try:
        names = sorted(os.listdir(root))
    except OSError:          # not Linux, or no DRM subsystem
        return out
    for n in names:
        if not (n.startswith("card") and n[4:].isdigit()):
            continue
        path = os.path.join(root, n)
        out.append((path, _read_sysfs(os.path.join(path, "device", "vendor"))))
    return out


def _read_sysfs(path: str) -> str | None:
    """One small sysfs read. Unreadable -> None, never an exception, never 0."""
    try:
        with open(path, encoding="utf-8") as f:
            v = f.read().strip()
        return v or None
    except OSError:
        return None


def _read_sysfs_int(path: str) -> int | None:
    v = _read_sysfs(path)
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _drm_vendors() -> set[str]:
    return {v for _, v in _drm_cards() if v}


# --- platform detection (cached) --------------------------------------------- #
_platform_id: str | None = None


def platform_id() -> str:
    """Coarse platform class, resolved once: 'linux-nvidia' | 'linux-amd' |
    'linux-intel' | 'linux-gpu' | 'linux-cpu' | 'darwin-apple' | 'darwin-intel' |
    'windows-nvidia' | 'windows' | 'generic'.

    Used to pick a GPU provider; memory probing is universal so it doesn't need
    this.

    NVIDIA is still detected by driver presence rather than by PCI id, because
    that is what actually predicts whether NVML answers. The DRM vendor scan only
    decides the *non*-NVIDIA classes — which is the gap this used to have: an AMD
    or Intel discrete card fell into `linux-cpu`, and `fit_memory()` then handed
    back system RAM as though it were the fit-limiting pool. On a 128 GB box with
    a 24 GB card that green-lit models three times too large.
    """
    global _platform_id
    if _platform_id is not None:
        return _platform_id
    sysname = sys.platform
    has_nvidia = _nvml() is not None or _which("nvidia-smi") is not None
    if sysname == "darwin":
        # Apple Silicon reports arm64; Intel Macs report x86_64.
        _platform_id = "darwin-apple" if _platform.machine() in (
            "arm64", "aarch64") else "darwin-intel"
    elif sysname.startswith("linux"):
        if has_nvidia:
            _platform_id = "linux-nvidia"
        else:
            vendors = _drm_vendors()
            if _VENDOR_AMD in vendors:
                _platform_id = "linux-amd"
            elif _VENDOR_INTEL in vendors:
                _platform_id = "linux-intel"
            elif _drm_cards():
                # A card exists but we cannot name its vendor. Not CPU-only, and
                # saying so is better than a confident wrong class.
                _platform_id = "linux-gpu"
            else:
                _platform_id = "linux-cpu"
    elif sysname.startswith("win"):
        _platform_id = "windows-nvidia" if has_nvidia else "windows"
    else:
        _platform_id = "generic"
    return _platform_id


def reset_cache() -> None:
    """Drop the platform and fit-memory caches. For tests and simulators.

    `tools/mac_sim_audit.py` and `tests/test_hwinfo.py` currently reach in and
    assign `hwinfo._platform_id = None` directly. That worked while the platform
    was one variable; it is a private-attribute poke that will rot now that an
    env override feeds the same decision, so give them a supported door. Mirrors
    `alloc/gpumem.reset_cache()`.
    """
    global _platform_id
    _platform_id = None
    _fit_cache.update(ts=0.0, info=None)


def _which(name: str) -> str | None:
    from shutil import which
    return which(name)


# --- system memory (unified/system RAM pool) --------------------------------- #
def system_mem() -> MemInfo:
    """Free/total of the OS memory pool — psutil (cross-platform) then /proc.

    This is the whole-box RAM the dashboard shows and, on unified-memory
    hardware (Spark, Apple Silicon), also the model fit-limiting resource.
    """
    if _psutil is not None:
        try:
            vm = _psutil.virtual_memory()
            g = 1024 ** 3
            return MemInfo(free_gb=vm.available / g, total_gb=vm.total / g,
                           source="system-psutil")
        except Exception:  # noqa: BLE001
            pass
    return _proc_meminfo()


def _proc_meminfo() -> MemInfo:
    """Linux /proc/meminfo fallback for when psutil is unavailable."""
    total = avail = None
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, _, v = line.partition(":")
                if not v:
                    continue
                if k == "MemTotal":
                    total = int(v.strip().split()[0]) / 1024 / 1024  # kB -> GiB
                elif k == "MemAvailable":
                    avail = int(v.strip().split()[0]) / 1024 / 1024
                if total is not None and avail is not None:
                    break
    except Exception:  # noqa: BLE001 — non-Linux / unreadable
        return MemInfo()
    if avail is None:
        return MemInfo()
    return MemInfo(free_gb=avail, total_gb=total, source="system-proc")


# --- discrete VRAM ----------------------------------------------------------- #
def vram_mem() -> MemInfo:
    """Free/total dedicated GPU VRAM summed across GPUs, or an empty MemInfo.

    Returns nothing on unified-memory hardware (GB10 reports N/A), on Apple
    Silicon (no NVIDIA), or without an NVIDIA driver — the fit-limiting resource
    there is system memory, resolved separately.
    """
    nv = _nvml()
    if nv is not None:
        try:
            free = total = 0.0
            for i in range(nv.nvmlDeviceGetCount()):
                h = nv.nvmlDeviceGetHandleByIndex(i)
                m = nv.nvmlDeviceGetMemoryInfo(h)
                free += m.free
                total += m.total
            if total > 0:
                g = 1024 ** 3
                return MemInfo(free_gb=free / g, total_gb=total / g,
                               source="vram-nvml")
        except Exception:  # noqa: BLE001 — fall through to nvidia-smi
            pass
    amd = _vram_amdgpu()
    if amd.readable:
        return amd
    return _vram_smi()


def _vram_amdgpu() -> MemInfo:
    """AMD VRAM from sysfs, summed across cards.

    Sysfs rather than `rocm-smi --json`, deliberately: this is an open() of a few
    bytes, so it costs microseconds, needs no root, and works inside a container
    that has no ROCm userspace installed — which is the ordinary case for a box
    running Ollama-ROCm from an image. `rocm-smi` is a Python script that can take
    over a second to answer and is frequently absent; it is worth shelling out to
    for a marketing *name* (see `_amd_gpus`), not for a number on the fit path.

    ⚠️ On an APU (Strix Halo, Phoenix) `mem_info_vram_total` is the BIOS UMA
    carve-out — often 512 MiB on `auto` — NOT the real pool. Returning it as
    "the fit-limiting resource" would refuse a 7B on a 128 GB machine, which is
    the exact mirror image of the bug this function was added to fix. That is why
    `fit_memory()` consults `_memory_model()` and not just these numbers.
    """
    free = total = 0.0
    saw = False
    for path, vendor in _drm_cards():
        if vendor != _VENDOR_AMD:
            continue
        dev = os.path.join(path, "device")
        t = _read_sysfs_int(os.path.join(dev, "mem_info_vram_total"))
        u = _read_sysfs_int(os.path.join(dev, "mem_info_vram_used"))
        if t is None:
            continue
        g = 1024 ** 3
        total += t / g
        free += (t - u) / g if u is not None else t / g
        saw = True
    return MemInfo(free, total, "vram-amdgpu-sysfs") if saw else MemInfo()


# --- unified vs discrete memory ---------------------------------------------- #
_MEMORY_MODEL_ENV = "AVA_GPU_MEMORY_MODEL"
# APU / integrated marketing names, lowercased substrings. A last resort behind
# the env override and the GTT ratio, both of which are properties rather than
# guesses about a product line.
_UNIFIED_NAME_HINTS = ("strix", "phoenix", "radeon 8060s", "radeon 890m",
                       "gfx115", "gfx1103", "ryzen ai max")


def _memory_model() -> tuple[str, str]:
    """`(model, why)` where model is 'unified' | 'discrete' | 'unknown'.

    Three signals, most trustworthy first — layered because the primary one is
    unverifiable on hardware the maintainer does not own:

    1. `AVA_GPU_MEMORY_MODEL=unified|discrete` — an operator override, so a
       misclassified box is a one-variable fix rather than a patch release.
    2. VRAM small relative to GTT. On an APU the shareable system aperture (GTT)
       dominates and VRAM is a carve-out; on a discrete card the reverse holds.
       This is a measured property of the device, not a name guess.
    3. A marketing-name allowlist, for APUs whose sysfs does not expose GTT.
    """
    env = (os.environ.get(_MEMORY_MODEL_ENV) or "").strip().lower()
    if env in ("unified", "discrete"):
        return env, f"{_MEMORY_MODEL_ENV}={env}"

    for path, vendor in _drm_cards():
        if vendor != _VENDOR_AMD:
            continue
        dev = os.path.join(path, "device")
        vram = _read_sysfs_int(os.path.join(dev, "mem_info_vram_total"))
        gtt = _read_sysfs_int(os.path.join(dev, "mem_info_gtt_total"))
        if vram and gtt and vram < 0.5 * gtt:
            return "unified", (f"vram {vram >> 20} MiB < half of gtt "
                               f"{gtt >> 20} MiB — integrated carve-out")
        if vram and gtt:
            return "discrete", (f"vram {vram >> 20} MiB dominates gtt "
                                f"{gtt >> 20} MiB")

    for g in _drm_names():
        low = g.lower()
        if any(h in low for h in _UNIFIED_NAME_HINTS):
            return "unified", f"name hint in {g!r}"
    return "unknown", "no APU signal"


def _drm_names() -> list[str]:
    """Best-effort card names from sysfs, for the name-hint fallback only."""
    out = []
    for path, _ in _drm_cards():
        dev = os.path.join(path, "device")
        for f in ("product_name", "marketing_name"):
            n = _read_sysfs(os.path.join(dev, f))
            if n:
                out.append(n)
                break
    return out


def _vram_smi() -> MemInfo:
    """`nvidia-smi` shell fallback for VRAM (also yields N/A on unified memory)."""
    smi = _which("nvidia-smi")
    if not smi:
        return MemInfo()
    try:
        out = subprocess.run(
            [smi, "--query-gpu=memory.free,memory.total",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=2.0)
    except Exception:  # noqa: BLE001
        return MemInfo()
    if out.returncode != 0 or not out.stdout.strip():
        return MemInfo()
    free = total = 0.0
    saw = False
    for line in out.stdout.strip().splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            continue
        try:  # "N/A" on unified memory raises -> skip
            free += float(parts[0]) / 1024   # MiB -> GiB
            total += float(parts[1]) / 1024
            saw = True
        except ValueError:
            continue
    return MemInfo(free, total, "vram-smi") if saw else MemInfo()


# --- the unified answer the fit layer wants ---------------------------------- #
# If a box has real *dedicated* VRAM, that's the fit-limiting pool. If VRAM reads
# back within ~15% of total system RAM, it's unified memory (Spark / Apple) being
# reported as VRAM, so we prefer the honest system-memory reading + label. TTL-
# cached so ordering every inference request costs ~nothing.
_FIT_TTL_S = float(os.environ.get("AVA_FIT_MEM_TTL", "3.0"))
_fit_cache: dict = {"ts": 0.0, "info": None}


def fit_memory() -> MemInfo:
    """The free memory the model-fit layer should gate on, hardware-adaptive.

    Discrete GPU -> free VRAM. Unified memory (Spark/Apple) or CPU-only -> free
    system RAM. Nothing readable (some non-Linux w/o psutil) -> empty MemInfo,
    which callers treat as 'unknown, don't gate'. Cached for _FIT_TTL_S.
    """
    now = time.time()
    cached = _fit_cache["info"]
    if cached is not None and now - _fit_cache["ts"] < _FIT_TTL_S:
        return cached
    sysm = system_mem()
    vram = vram_mem()
    info = sysm
    if vram.readable:
        # Two ways a box can be unified, and they need different signals.
        #
        # NVIDIA Grace-class reports its unified pool AS VRAM, so VRAM ≈ system
        # RAM and the ratio test catches it. That path is unchanged and is what
        # keeps the maintainer's GB10 correct.
        #
        # An AMD APU does the opposite: it reports a *tiny* BIOS carve-out as
        # VRAM (512 MiB on `auto`) while the real pool is system RAM. The ratio
        # test cannot see that — 0.5 GB is nowhere near 0.85 × 128 GB — so it
        # would fall through to `vram` and gate every model against 512 MiB.
        # `_memory_model()` asks the device instead of inferring from size.
        model, _why = _memory_model()
        unified = model == "unified" or (
            model != "discrete" and sysm.total_gb and vram.total_gb
            and vram.total_gb >= 0.85 * sysm.total_gb)
        info = sysm if (unified and sysm.readable) else vram
    # KNOWN GAP, deliberately not "fixed" here — see the note below.
    #
    # When VRAM is unreadable we fall through to system RAM. That is CORRECT on
    # GB10/Grace (vram_mem's docstring: "Returns nothing on unified-memory
    # hardware (GB10 reports N/A)"), on Apple Silicon and on CPU-only boxes. It is
    # WRONG on a discrete NVIDIA/AMD box whose probe merely FAILED — driver
    # mismatch, no permission on /dev/nvidia*, a container without the toolkit —
    # where a 128 GB host with an 8 GB card advertises 128 GB of fit memory and
    # platforms.py:203-206 then labels it `unified` because the source string
    # begins with "system", so Setup recommends a tier that OOMs on first load.
    #
    # Gating on platform_id() does not separate the two: both are linux-nvidia
    # with an empty MemInfo, and `_memory_model()` reads amdgpu sysfs so it
    # answers ("unknown", "no APU signal") on Grace. Doing it properly means
    # teaching vram_mem() to distinguish "the probe answered N/A" from "the probe
    # errored" and gating on THAT — a change only verifiable on a discrete NVIDIA
    # box, which is why it is written down rather than guessed at.
    # tests/test_platform_native.py::test_unified_memory_nvidia_falls_through_to_
    # the_system_pool pins the current, correct-for-GB10 behaviour.
    _fit_cache.update(ts=now, info=info)
    return info


# --- GPU telemetry providers ------------------------------------------------- #
def gpus() -> list[GpuInfo]:
    """Live telemetry for each accelerator. Empty list if none is readable.

    Ordered by how much each provider can actually tell us, then a name-only
    floor: a box with a readable GPU should never report `[]` just because we
    lack a rich provider for it. `[]` used to mean both "no accelerator" and
    "an accelerator we don't have code for", which blanked the dashboard bubble
    and all four Vitals gauges on hardware that was working fine.
    """
    pid = platform_id()
    if pid in ("linux-nvidia", "windows-nvidia"):
        got = _nvidia_gpus()
        if got:
            return got
    if pid == "darwin-apple":
        return _apple_gpus()
    if pid in ("linux-amd", "linux-intel", "linux-gpu"):
        got = _amd_gpus() + _intel_gpus()
        if got:
            return got
    return _drm_floor_gpus()


def _amd_gpus() -> list[GpuInfo]:
    """AMD telemetry from sysfs — unprivileged, no ROCm userspace required.

    `power1_average` is in microwatts and is the one field that matters most
    here: it is what lets `dashboard.perf_cost` report measured energy instead of
    substituting `nominal_gpu_watts`. Every field is independently optional, so a
    kernel that exposes power but not utilisation still yields a useful row.
    """
    out = []
    for path, vendor in _drm_cards():
        if vendor != _VENDOR_AMD:
            continue
        dev = os.path.join(path, "device")
        total = _read_sysfs_int(os.path.join(dev, "mem_info_vram_total"))
        used = _read_sysfs_int(os.path.join(dev, "mem_info_vram_used"))
        g = 1024 ** 3
        out.append(GpuInfo(
            name=_amd_name(dev),
            util=_f(_read_sysfs(os.path.join(dev, "gpu_busy_percent"))),
            temp_c=_hwmon_scaled(dev, "temp1_input", 1000.0),
            power_w=_hwmon_scaled(dev, "power1_average", 1_000_000.0),
            mem_used_gb=(used / g) if used is not None else None,
            mem_total_gb=(total / g) if total is not None else None,
            source="amdgpu-sysfs"))
    return out


def _amd_name(dev: str) -> str | None:
    for f in ("product_name", "marketing_name"):
        n = _read_sysfs(os.path.join(dev, f))
        if n:
            return n
    return "AMD GPU"


def _hwmon_scaled(dev: str, leaf: str, divisor: float) -> float | None:
    """Read `<dev>/hwmon/hwmon*/<leaf>` and scale it. None if absent.

    The hwmon index is not stable across boots, so it is globbed rather than
    assumed.
    """
    base = os.path.join(dev, "hwmon")
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for n in names:
        v = _read_sysfs_int(os.path.join(base, n, leaf))
        if v is not None:
            return v / divisor
    return None


def _intel_gpus() -> list[GpuInfo]:
    """Intel telemetry. `xpu-smi` where present, else the sysfs floor.

    Intel is not one of the four first-class platform targets, so this is
    deliberately the reader and nothing more — no launcher, no flag table. It
    exists so an Arc box reports its GPU instead of reporting none.
    """
    have_intel = any(v == _VENDOR_INTEL for _, v in _drm_cards())
    if not have_intel:
        return []
    out = []
    for path, vendor in _drm_cards():
        if vendor != _VENDOR_INTEL:
            continue
        dev = os.path.join(path, "device")
        out.append(GpuInfo(
            name=_read_sysfs(os.path.join(dev, "product_name")) or "Intel GPU",
            power_w=_hwmon_scaled(dev, "power1_average", 1_000_000.0),
            temp_c=_hwmon_scaled(dev, "temp1_input", 1000.0),
            source="intel-sysfs"))
    return out


def _drm_floor_gpus() -> list[GpuInfo]:
    """Last resort: a card exists, so say so, with whatever name we can find.

    Returns `[]` only when the box genuinely has no DRM card. Every numeric field
    is None, which callers already render as `—` (see HardwareBubble.tsx) rather
    than as zero.
    """
    out = []
    for path, vendor in _drm_cards():
        dev = os.path.join(path, "device")
        name = (_read_sysfs(os.path.join(dev, "product_name"))
                or _vendor_label(vendor))
        out.append(GpuInfo(name=name, source="drm-sysfs"))
    return out


def _vendor_label(vendor: str | None) -> str:
    return {_VENDOR_AMD: "AMD GPU", _VENDOR_INTEL: "Intel GPU",
            _VENDOR_NVIDIA: "NVIDIA GPU"}.get(vendor or "", "GPU")


_RAPL_ROOT = "/sys/class/powercap"


def _have_rapl() -> bool:
    """Is there an actually-readable CPU energy counter?

    Not `isdir(_RAPL_ROOT)`: the directory is present and empty on ARM, so the
    directory test answers "yes" where the answer is "nothing to read".
    """
    try:
        names = os.listdir(_RAPL_ROOT)
    except OSError:
        return False
    return any(_read_sysfs_int(os.path.join(_RAPL_ROOT, n, "energy_uj"))
               is not None for n in names)


def gpu() -> GpuInfo:
    """First/primary accelerator (convenience for single-GPU callers)."""
    g = gpus()
    return g[0] if g else GpuInfo()


def _nvidia_gpus() -> list[GpuInfo]:
    """NVML-preferred NVIDIA telemetry, `nvidia-smi` shell fallback."""
    nv = _nvml()
    if nv is not None:
        try:
            out = []
            g = 1024 ** 3
            for i in range(nv.nvmlDeviceGetCount()):
                h = nv.nvmlDeviceGetHandleByIndex(i)
                m = nv.nvmlDeviceGetMemoryInfo(h)
                out.append(GpuInfo(
                    name=_nvml_name(nv, h),
                    util=_nvml_try(lambda: nv.nvmlDeviceGetUtilizationRates(h).gpu),
                    temp_c=_nvml_try(lambda: nv.nvmlDeviceGetTemperature(
                        h, nv.NVML_TEMPERATURE_GPU)),
                    power_w=_nvml_try(lambda: nv.nvmlDeviceGetPowerUsage(h) / 1000.0),
                    mem_used_gb=(m.used / g) if m.total else None,
                    mem_total_gb=(m.total / g) if m.total else None,
                    source="nvml"))
            if out:
                return out
        except Exception:  # noqa: BLE001 — fall through to smi
            pass
    return _nvidia_gpus_smi()


def _nvml_name(nv, h) -> str | None:
    try:
        n = nv.nvmlDeviceGetName(h)
        return n.decode() if isinstance(n, bytes) else n
    except Exception:  # noqa: BLE001
        return None


def _nvml_try(fn):
    try:
        return fn()
    except Exception:  # noqa: BLE001 — not all fields exist on all GPUs
        return None


def _nvidia_gpus_smi() -> list[GpuInfo]:
    smi = _which("nvidia-smi")
    if not smi:
        return []
    try:
        r = subprocess.run(
            [smi, "--query-gpu=name,utilization.gpu,temperature.gpu,power.draw,"
                  "memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=4)
    except Exception:  # noqa: BLE001
        return []
    out = []
    for line in (r.stdout or "").strip().splitlines():
        p = [x.strip() for x in line.split(",")]
        if len(p) < 6:
            continue
        out.append(GpuInfo(
            name=p[0] or None, util=_f(p[1]), temp_c=_f(p[2]), power_w=_f(p[3]),
            mem_used_gb=_gb(_f(p[4])), mem_total_gb=_gb(_f(p[5])),
            source="nvidia-smi"))
    return out


def _apple_gpus() -> list[GpuInfo]:
    """Apple Silicon GPU. Memory is unified (see system_mem); util/temp/power have
    no unprivileged API (powermetrics needs sudo, Metal counters are private), so
    those stay None rather than fabricated. Name comes from the SoC brand string.
    """
    name = _run(["sysctl", "-n", "machdep.cpu.brand_string"]) or "Apple Silicon"
    sysm = system_mem()
    return [GpuInfo(
        name=f"{name} GPU", mem_used_gb=(
            (sysm.total_gb - sysm.free_gb) if sysm.readable else None),
        mem_total_gb=sysm.total_gb if sysm.readable else None,
        source="apple")]


# --- small helpers ----------------------------------------------------------- #
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _gb(mib):
    return round(mib / 1024, 2) if mib is not None else None


def _run(cmd: list[str], timeout: int = 3) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def snapshot() -> dict:
    """Everything the HAL knows, for /fit, the dashboard, and `ava doctor`."""
    fm = fit_memory()
    sm = system_mem()
    return {
        "platform": platform_id(),
        "fit_memory": {"free_gb": _r(fm.free_gb), "total_gb": _r(fm.total_gb),
                       "source": fm.source},
        "system_memory": {"free_gb": _r(sm.free_gb), "total_gb": _r(sm.total_gb),
                          "source": sm.source},
        "gpus": [g.__dict__ for g in gpus()],
        "memory_model": dict(zip(("model", "why"), _memory_model())),
        # Which readers this box actually has, so `ava doctor` can say why a
        # field is None instead of leaving the user to guess.
        "have": {
            "psutil": _psutil is not None,
            "nvml": _nvml() is not None,
            "nvidia_smi": _which("nvidia-smi") is not None,
            "drm": bool(_drm_cards()),
            "amdgpu_sysfs": _VENDOR_AMD in _drm_vendors(),
            "intel_sysfs": _VENDOR_INTEL in _drm_vendors(),
            "rocm_smi": _which("rocm-smi") is not None,
            "xpu_smi": _which("xpu-smi") is not None,
            # Effectively x86-only. Note this must test for a real counter, not
            # for the directory: on this aarch64 box /sys/class/powercap EXISTS
            # and is EMPTY, so `os.path.isdir` reported RAPL available when there
            # was nothing to read — a false positive of exactly the kind the
            # `None means unknown, never zero` rule at the top of this module is
            # meant to prevent.
            "rapl": _have_rapl(),
        },
    }


def _r(v):
    return round(v, 1) if isinstance(v, (int, float)) else v
