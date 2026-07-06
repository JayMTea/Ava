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


# --- platform detection (cached) --------------------------------------------- #
_platform_id: str | None = None


def platform_id() -> str:
    """Coarse platform class, resolved once: 'linux-nvidia' | 'linux-cpu' |
    'darwin-apple' | 'darwin-intel' | 'windows-nvidia' | 'windows' | 'generic'.
    Used to pick a GPU provider; memory probing is universal so it doesn't need this.
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
        _platform_id = "linux-nvidia" if has_nvidia else "linux-cpu"
    elif sysname.startswith("win"):
        _platform_id = "windows-nvidia" if has_nvidia else "windows"
    else:
        _platform_id = "generic"
    return _platform_id


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
    return _vram_smi()


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
        # Distinguish dedicated VRAM from unified-memory-reported-as-VRAM.
        unified = (sysm.total_gb and vram.total_gb
                   and vram.total_gb >= 0.85 * sysm.total_gb)
        info = sysm if (unified and sysm.readable) else vram
    _fit_cache.update(ts=now, info=info)
    return info


# --- GPU telemetry providers ------------------------------------------------- #
def gpus() -> list[GpuInfo]:
    """Live telemetry for each accelerator. Empty list if none is readable."""
    pid = platform_id()
    if pid in ("linux-nvidia", "windows-nvidia"):
        got = _nvidia_gpus()
        if got:
            return got
    if pid == "darwin-apple":
        return _apple_gpus()
    # linux-cpu / windows / darwin-intel / generic: no readable accelerator.
    return []


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
        "have": {"psutil": _psutil is not None, "nvml": _nvml() is not None},
    }


def _r(v):
    return round(v, 1) if isinstance(v, (int, float)) else v
