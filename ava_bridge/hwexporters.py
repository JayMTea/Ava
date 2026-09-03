"""Hardware read from ANOTHER machine, through the Prometheus exporters it runs.

Every reader in `hwinfo.py` and `hardware.py` describes the box this process is
on. That is right for a bare-metal install and wrong for the other common
shape: the bridge on a small always-on host (a NAS, a mini PC) and the models on
the machine with the GPU. There the floating monitor reported the NAS's memory
and a CPU with no GPU behind it, and the model-fit layer sized recommendations
against the wrong machine's RAM - confidently, with no hint that it was looking
at the wrong box.

The fix is not a new telemetry agent. Anyone running a GPU box for inference
already has, or can install in one command, the two standard exporters:

    node_exporter   memory, CPU counters, filesystems   (any Linux host)
    dcgm-exporter   GPU utilisation, temperature, power, framebuffer   (NVIDIA)
    nvidia_gpu_exporter  the same four from nvidia-smi, for boxes without DCGM

This module reads their `/metrics` pages directly - not through Prometheus, so
the numbers are live rather than a scrape interval old, and the monitor keeps
working when the metrics stack is down - and maps them onto the HAL's own
`MemInfo` / `GpuInfo` shapes. `hwinfo` consults `reading()` at the top of each
public reader, so the fit router, the allocation governor, Setup -> Hardware,
`ava doctor` and the monitor all follow the configured box with one switch.

Two rules this module keeps that the rest of the HAL already keeps:

  * **Never a number from the wrong machine.** When the exporters do not answer,
    every reading is None and the snapshot says so with a `remote_hardware_down`
    code. Falling back to this host's figures would reproduce, silently, the
    exact bug this exists to end.
  * **None means unknown, never zero.** A metric the exporter does not export
    (DCGM's framebuffer fields on a unified-memory GB10) is absent, and absent
    stays None all the way to the UI.

It is a registry capability (`features.REGISTRY["remote_hardware"]`), so the
switch, the Setup checkbox, the `_off` / `_down` codes and the chat's fix-it
links all come from the one entry. Addresses live under `hardware.exporters`
in ava.yaml and are edited in Setup -> Hardware.
"""
from __future__ import annotations

import math
import re
import threading
import time
from dataclasses import dataclass, field, replace
from urllib.parse import urlparse

import requests

from . import features, settings
from .hwinfo import (PROBE_MEASURED, PROBE_NA, PROBE_NONE, PROBE_UNREADABLE,
                     GpuInfo, MemInfo)

KEY = "remote_hardware"                 # the features.REGISTRY entry

NODE_URL_ENV = "AVA_NODE_EXPORTER_URL"
GPU_URL_ENV = "AVA_GPU_EXPORTER_URL"
MOUNT_ENV = "AVA_EXPORTER_DISK_MOUNT"
LABEL_ENV = "AVA_HARDWARE_LABEL"

# The monitor polls every 2 s from every open client and each poll reads memory,
# GPU, CPU and disk in turn; one fetch per second is shared across all of them.
_TTL_S = 1.0
_DEFAULT_TIMEOUT_S = 1.5
# A node_exporter page is ~100 KB, a busy box a few MB. Anything past this is
# not an exporter answering, so it is refused rather than parsed.
_MAX_BYTES = 8 * 1024 * 1024
_GIB = 1024 ** 3

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #


def config() -> dict:
    """The `hardware.exporters` block, env overrides applied, defaults filled."""
    def pick(key: str, env: str | None, default: str = "") -> str:
        v = settings.get(f"hardware.exporters.{key}", None, env=env)
        return str(v).strip() if isinstance(v, (str, int, float)) else default
    mount = pick("disk_mount", MOUNT_ENV) or "/"
    return {
        "label": pick("label", LABEL_ENV),
        "node_url": pick("node_url", NODE_URL_ENV),
        "gpu_url": pick("gpu_url", GPU_URL_ENV),
        "disk_mount": mount,
        "timeout_s": settings.get_float("hardware.exporters.timeout_s",
                                        _DEFAULT_TIMEOUT_S),
    }


def configured() -> bool:
    """Is there anywhere to read from? Either exporter alone is a valid setup:
    a CPU-only box has no GPU exporter, and a box whose node_exporter is not
    reachable can still show its GPU."""
    c = config()
    return bool(c["node_url"] or c["gpu_url"])


def validate_url(url: str) -> str:
    """"" when `url` is an acceptable exporter address, else why not."""
    if not url:
        return ""
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        return "must start with http:// or https://"
    if not p.hostname:
        return "needs a host name or address"
    return ""


# --------------------------------------------------------------------------- #
# The Prometheus text format
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class Sample:
    labels: dict[str, str]
    value: float


Metrics = dict[str, list[Sample]]      # metric family name -> its samples

_LINE = re.compile(r"^([A-Za-z_:][A-Za-z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?\s*$")
_LABEL = re.compile(r'([A-Za-z_][A-Za-z0-9_]*)="((?:[^"\\]|\\.)*)"')


def _unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace('\\"', '"').replace("\\\\", "\\")


def parse_metrics(text: str, want: frozenset[str] | None = None) -> Metrics:
    """Parse exposition text into `{name: [Sample, ...]}`.

    `want` keeps only those families - the cheap prefix test runs before the
    regex, which is what keeps a multi-megabyte page cheap to read every second.
    NaN and infinities are dropped: an exporter says NaN for "no reading", and
    the HAL's word for that is None.
    """
    out: Metrics = {}
    for line in text.splitlines():
        if not line or line[0] == "#":
            continue
        brace = line.find("{")
        space = line.find(" ")
        end = min(x for x in (brace, space) if x >= 0) if (brace >= 0 or space >= 0) else -1
        name = line[:end] if end >= 0 else line
        if want is not None and name not in want:
            continue
        m = _LINE.match(line)
        if not m:
            continue
        name, raw_labels, raw_value = m.groups()
        try:
            value = float(raw_value)
        except ValueError:
            continue
        if math.isnan(value) or math.isinf(value):
            continue
        labels: dict[str, str] = {}
        if raw_labels:
            for lm in _LABEL.finditer(raw_labels):
                labels[lm.group(1)] = _unescape(lm.group(2))
        out.setdefault(name, []).append(Sample(labels, value))
    return out


def _one(metrics: Metrics | None, name: str) -> float | None:
    """The value of a single-sample family, else None."""
    if not metrics:
        return None
    rows = metrics.get(name) or []
    return rows[0].value if rows else None


# --------------------------------------------------------------------------- #
# node_exporter: memory, CPU, filesystems, the host's name
# --------------------------------------------------------------------------- #

_NODE_WANT = frozenset({
    "node_memory_MemTotal_bytes", "node_memory_MemAvailable_bytes",
    "node_cpu_seconds_total",
    "node_filesystem_size_bytes", "node_filesystem_free_bytes",
    "node_filesystem_avail_bytes",
    "node_uname_info",
})


def system_mem(r: "Reading") -> MemInfo:
    """Whole-box RAM, the way `hwinfo.system_mem()` reports it locally."""
    total = _one(r.node, "node_memory_MemTotal_bytes")
    avail = _one(r.node, "node_memory_MemAvailable_bytes")
    if not total or avail is None:
        return MemInfo()
    return MemInfo(free_gb=avail / _GIB, total_gb=total / _GIB,
                   source="system-node-exporter")


def _cpu_counters(metrics: Metrics | None) -> tuple[float, float] | None:
    """(idle+iowait seconds, all seconds) summed across CPUs, or None."""
    rows = (metrics or {}).get("node_cpu_seconds_total")
    if not rows:
        return None
    idle = total = 0.0
    for s in rows:
        total += s.value
        if s.labels.get("mode") in ("idle", "iowait"):
            idle += s.value
    return idle, total


def _cpu_util(now: tuple[float, float] | None, prev: tuple[float, float] | None) -> float | None:
    """Utilisation % between two counter snapshots - the /proc/stat arithmetic
    `hardware._cpu()` does locally, over a longer window. None until there are
    two distinct snapshots to difference."""
    if now is None or prev is None:
        return None
    dt = now[1] - prev[1]
    di = now[0] - prev[0]
    if dt <= 0:
        return None
    return round(max(0.0, min(100.0, 100 * (dt - di) / dt)))


def _mount_for(path: str, mountpoints: list[str]) -> str | None:
    """The mountpoint holding `path`: exact, else the longest mount that is a
    prefix of it - so an owner may name the models directory rather than the
    mount, the way AVA_HOME is a directory and not a filesystem."""
    if path in mountpoints:
        return path
    norm = path.rstrip("/") + "/"
    best = None
    for mp in mountpoints:
        pref = mp.rstrip("/") + "/"
        if norm.startswith(pref) and (best is None or len(mp) > len(best)):
            best = mp
    return best


def disk(r: "Reading") -> dict:
    """The filesystem on the remote box that holds the models, in the shape
    `hardware._disk()` returns. `capped` is False by construction: an exporter on
    the host reports the host's own volume, not a container's slice of it."""
    path = config()["disk_mount"]
    base = {"total_gb": None, "used_gb": None, "free_gb": None, "used_pct": None,
            "path": path, "capped": False, "cap_kind": None}
    if not r.node:
        return base
    sizes = {s.labels.get("mountpoint", ""): s.value
             for s in r.node.get("node_filesystem_size_bytes", [])}
    mp = _mount_for(path, [m for m in sizes if m])
    if mp is None or not sizes.get(mp):
        return base

    def val(name: str) -> float | None:
        for s in r.node.get(name, []):
            if s.labels.get("mountpoint") == mp:
                return s.value
        return None
    size = sizes[mp]
    free = val("node_filesystem_free_bytes")
    avail = val("node_filesystem_avail_bytes")
    if free is None:
        free = avail
    if free is None:
        return base
    used = max(0.0, size - free)
    return {"total_gb": round(size / _GIB, 1), "used_gb": round(used / _GIB, 1),
            "free_gb": round((avail if avail is not None else free) / _GIB, 1),
            "used_pct": round(100 * used / size) if size else None,
            "path": mp, "capped": False, "cap_kind": None}


def hostname(r: "Reading") -> str:
    """What the remote box calls itself, from node_exporter's uname family."""
    for s in (r.node or {}).get("node_uname_info", []):
        n = s.labels.get("nodename", "").strip()
        if n:
            return n
    return ""


# --------------------------------------------------------------------------- #
# GPU exporters
# --------------------------------------------------------------------------- #
# One table per exporter: which family carries each field and how to scale it
# into the HAL's units (% / degC / W / GB). Adding an exporter is one more row
# here and nothing else in the app changes - the same promise hwinfo makes for a
# new accelerator provider.
_MIB_TO_GB = 1 / 1024
_B_TO_GB = 1 / _GIB
_GPU_SCHEMAS: tuple[dict, ...] = (
    {   # NVIDIA DCGM exporter (nvcr.io/nvidia/k8s/dcgm-exporter). Framebuffer
        # fields are in MiB and are ABSENT on unified-memory parts (GB10, Grace).
        "id": "dcgm", "index": "gpu", "name": "modelName",
        "util": ("DCGM_FI_DEV_GPU_UTIL", 1.0),
        "temp": ("DCGM_FI_DEV_GPU_TEMP", 1.0),
        "power": ("DCGM_FI_DEV_POWER_USAGE", 1.0),
        "mem_used": ("DCGM_FI_DEV_FB_USED", _MIB_TO_GB),
        "mem_free": ("DCGM_FI_DEV_FB_FREE", _MIB_TO_GB),
        "mem_total": ("DCGM_FI_DEV_FB_TOTAL", _MIB_TO_GB),
    },
    {   # nvidia_gpu_exporter (utkuozdemir): nvidia-smi fields, ratio + bytes.
        "id": "nvidia-smi-exporter", "index": "uuid", "name": "name",
        "util": ("nvidia_smi_utilization_gpu_ratio", 100.0),
        "temp": ("nvidia_smi_temperature_gpu", 1.0),
        "power": ("nvidia_smi_power_draw_watts", 1.0),
        "mem_used": ("nvidia_smi_memory_used_bytes", _B_TO_GB),
        "mem_free": ("nvidia_smi_memory_free_bytes", _B_TO_GB),
        "mem_total": ("nvidia_smi_memory_total_bytes", _B_TO_GB),
    },
)
_GPU_FIELDS = ("util", "temp", "power", "mem_used", "mem_free", "mem_total")
_GPU_WANT = frozenset(s[f][0] for s in _GPU_SCHEMAS for f in _GPU_FIELDS)


def _gpu_schema(metrics: Metrics | None) -> dict | None:
    """Which exporter answered, by which families are present."""
    if not metrics:
        return None
    for schema in _GPU_SCHEMAS:
        if any(schema[f][0] in metrics for f in _GPU_FIELDS):
            return schema
    return None


def gpus(r: "Reading") -> list[GpuInfo]:
    """One `GpuInfo` per accelerator the exporter reports. Empty when it did
    not answer or speaks an exporter this table does not know."""
    schema = _gpu_schema(r.gpu)
    if schema is None:
        return []
    by_dev: dict[str, dict] = {}
    for f in _GPU_FIELDS:
        fam, scale = schema[f]
        for s in (r.gpu or {}).get(fam, []):
            key = s.labels.get(schema["index"], "")
            d = by_dev.setdefault(key, {"name": s.labels.get(schema["name"])})
            d[f] = s.value * scale
            if not d["name"]:
                d["name"] = s.labels.get(schema["name"])
    out = []
    for key in sorted(by_dev):
        d = by_dev[key]
        total = d.get("mem_total")
        if total is None and d.get("mem_used") is not None and d.get("mem_free") is not None:
            total = d["mem_used"] + d["mem_free"]
        # A framebuffer of zero is DCGM's spelling of "no dedicated pool" on
        # unified-memory parts; the HAL's is None.
        if not total:
            total, used = None, None
        else:
            used = d.get("mem_used")
        out.append(GpuInfo(name=d.get("name") or None, util=d.get("util"),
                           temp_c=d.get("temp"), power_w=d.get("power"),
                           mem_used_gb=used, mem_total_gb=total,
                           source=schema["id"]))
    return out


def vram_probe(r: "Reading") -> tuple[MemInfo, str]:
    """Dedicated GPU memory summed across the box's cards, plus the four-way
    status `hwinfo.vram_probe()` promises - the value `fit_pool()` keys on."""
    if not config()["gpu_url"]:
        # No GPU exporter named. NONE rather than UNREADABLE: the owner said this
        # box has no accelerator to read, which is a statement, not a failure.
        return MemInfo(), PROBE_NONE
    if r.gpu is None or _gpu_schema(r.gpu) is None:
        return MemInfo(), PROBE_UNREADABLE
    cards = gpus(r)
    total = sum(g.mem_total_gb for g in cards if g.mem_total_gb)
    if total <= 0:
        return MemInfo(), PROBE_NA          # answered, and no separate pool: unified
    used = sum(g.mem_used_gb or 0.0 for g in cards)
    schema = _gpu_schema(r.gpu) or {}
    return (MemInfo(free_gb=max(0.0, total - used), total_gb=total,
                    source=f"vram-{schema.get('id', 'exporter')}"), PROBE_MEASURED)


# --------------------------------------------------------------------------- #
# Fetching, caching, gating
# --------------------------------------------------------------------------- #

@dataclass
class Reading:
    """One fetch of every configured exporter, plus how it went.

    `state`: unconfigured (no addresses) | off (switch is off) | ok | down.
    `down` still carries whatever DID answer - a GPU exporter that is up while
    node_exporter is down should not blank the GPU meters - but the snapshot
    reports the failure and the fix-it link, and nothing falls back to this
    host's own readers.
    """
    state: str = "unconfigured"
    code: str = ""
    error: str = ""
    node: Metrics | None = None
    gpu: Metrics | None = None
    node_error: str = ""
    gpu_error: str = ""
    cpu_util: float | None = None
    ts: float = 0.0
    labels: dict = field(default_factory=dict)


_lock = threading.Lock()
_state: dict = {"reading": None, "cpu_prev": None}


def reset_cache() -> None:
    """Forget the last fetch and the CPU counters. For tests and config saves."""
    with _lock:
        _state.update(reading=None, cpu_prev=None)


def _http_get(url: str, timeout: float) -> tuple[int, str]:
    """(status, body). The one network call, so a test replaces one function."""
    resp = requests.get(url, timeout=timeout, headers={"Accept": "text/plain"})
    body = resp.content
    if len(body) > _MAX_BYTES:
        raise ValueError(f"answered with {len(body) // (1024 * 1024)} MB, which is "
                         "not an exporter page")
    return resp.status_code, body.decode("utf-8", "replace")


def _get(url: str, what: str, timeout: float,
         want: frozenset[str]) -> tuple[Metrics | None, str]:
    """Fetch and parse one exporter. On failure, (None, an actionable sentence)."""
    try:
        status, text = _http_get(url, timeout)
    except requests.exceptions.Timeout:
        return None, f"{what} at {url} did not answer within {timeout:g}s"
    except requests.exceptions.ConnectionError:
        return None, f"{what} at {url} could not be reached"
    except Exception as e:  # noqa: BLE001 - any other failure is still "did not answer"
        return None, f"{what} at {url}: {e}"
    if status != 200:
        return None, f"{what} at {url} answered HTTP {status}"
    metrics = parse_metrics(text, want)
    if not metrics:
        return None, f"{what} at {url} answered, but with none of the metrics Ava reads"
    return metrics, ""


def _fetch() -> Reading:
    """The cached reading of every configured exporter (one fetch per TTL)."""
    with _lock:
        now = time.time()
        cached = _state["reading"]
        if cached is not None and now - cached.ts < _TTL_S:
            return cached
        cfg = config()
        r = Reading(state="ok", ts=now)
        if cfg["node_url"]:
            r.node, r.node_error = _get(cfg["node_url"], "node_exporter",
                                        cfg["timeout_s"], _NODE_WANT)
        if cfg["gpu_url"]:
            r.gpu, r.gpu_error = _get(cfg["gpu_url"], "the GPU exporter",
                                      cfg["timeout_s"], _GPU_WANT)
        errs = [e for e in (r.node_error, r.gpu_error) if e]
        if errs:
            r.state, r.error = "down", "; ".join(errs)
        counters = _cpu_counters(r.node)
        if counters is not None:
            r.cpu_util = _cpu_util(counters, _state["cpu_prev"])
            _state["cpu_prev"] = counters
        _state["reading"] = r
        return r


def _probe() -> str | None:
    """features.preflight's probe: None when every exporter answered."""
    r = _fetch()
    return r.error or None


def reading() -> Reading:
    """The remote reading, gated the way every registry capability is gated.

    Off never fetches (preflight does not call the probe), so a configured-but-
    disabled box costs nothing on the monitor's poll. On-and-down still returns
    the partial reading so whatever answered is shown, with the `_down` code and
    the probe's sentence attached for the UI.
    """
    if not configured():
        return Reading(state="unconfigured")
    verdict = features.preflight(KEY, probe=_probe)
    if verdict is None:
        return _fetch()
    code, msg = verdict
    if code.endswith("_off"):
        return Reading(state="off", code=code, error=msg)
    return replace(_fetch(), code=code, error=msg)


def active(r: Reading | None = None) -> bool:
    """Is the remote box THE hardware source right now? True whether or not it
    is answering: once the owner points Ava elsewhere and switches it on, this
    host's own readers are the wrong machine either way."""
    r = reading() if r is None else r
    return r.state in ("ok", "down")


def label(r: Reading | None = None) -> str:
    """What to call the remote box: the owner's label, else its own hostname,
    else the host part of an exporter address."""
    r = reading() if r is None else r
    cfg = config()
    if cfg["label"]:
        return cfg["label"]
    h = hostname(r)
    if h:
        return h
    for url in (cfg["node_url"], cfg["gpu_url"]):
        host = urlparse(url).hostname if url else None
        if host:
            return host
    return "remote machine"


def describe(r: Reading | None = None) -> dict:
    """The fact the monitor and Setup render: whose hardware a snapshot is.

    `kind` is `local` or `exporters`; `reachable` is whether every configured
    exporter answered; `error_code` / `error` carry the registry's regular codes
    (`remote_hardware_off` on a local reading means "you configured a remote box
    and switched it off" - the monitor can say so and link to the switch).
    """
    r = reading() if r is None else r
    if r.state == "unconfigured":
        return {"kind": "local", "label": "", "reachable": True,
                "error_code": "", "error": ""}
    if r.state == "off":
        return {"kind": "local", "label": "", "reachable": True,
                "error_code": r.code, "error": r.error}
    return {"kind": "exporters", "label": label(r), "reachable": r.state == "ok",
            "error_code": r.code, "error": r.error}
