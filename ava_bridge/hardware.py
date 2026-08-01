"""Live hardware stats for the app's floating monitor.

GPU/CPU/memory telemetry comes from the hardware abstraction layer
(ava_bridge/hwinfo.py), so the monitor works across NVIDIA (Linux), the GB10
(unified memory; VRAM reads N/A), Apple Silicon (unified memory; no nvidia-smi),
and CPU-only boxes. On unified-memory hardware the GPU shares the system RAM
pool, so `_mem()` (system memory) is the honest "unified memory" figure. Per-
process GPU inventory below is still NVIDIA/Linux-specific (nvidia-smi compute
apps) and degrades to empty elsewhere. Everything is best-effort: any unavailable
field is None so the UI shows a dash instead of failing.
"""
import shutil
import subprocess
import threading
import time
import json
import re
import os
from collections import deque
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import hwinfo

try:  # psutil powers cross-platform CPU%; /proc/stat is the Linux fallback.
    import psutil as _psutil
except Exception:  # noqa: BLE001
    _psutil = None

_MAPS_CACHE: dict[int, dict] = {}


def _short_model_name(model: str | None, runtime: str | None = None) -> str:
    """Display name for a model, derived from its actual id — never assumed.

    No hardcoded model identities here: a name we can't read stays a generic
    runtime-flavoured label, so a fork serving any model is labeled honestly.
    Friendly display labels come from the backend config (see _loaded_models),
    not from pattern-matching ids.
    """
    m = (model or "").strip()
    ml = m.lower()
    rl = (runtime or "").lower()

    # Worker-process names and placeholders carry no model identity.
    is_placeholder = (
        ml in {"", "(unavailable)", "(model not exposed)", "vllm::enginecore", "enginecore",
               "python", "python3"}
        or ml.endswith(("/python", "/python3"))
    )
    if is_placeholder:
        if "gpusvc" in rl or "gpusvc" in ml:
            return "the GPU service"
        if any(k in rl or k in ml for k in ("vllm", "enginecore")):
            return "vLLM model"
        return "Model"

    # HF-style org/name ids and file paths: the tail segment is the name.
    m = m.replace("\\", "/").split("/")[-1]
    if _is_model_file(m):
        m = m.rsplit(".", 1)[0]
    return m


def _short_runtime_name(name: str | None, model: str | None = None) -> str:
    n = (name or "").strip()
    nl = n.lower()
    ml = (model or "").lower()

    if "gpusvc" in nl or "gpusvc" in ml:
        return "the GPU service"
    if "vllm" in nl or "enginecore" in nl:
        return "vLLM"
    if "ollama" in nl:
        return "Ollama"
    if "python" in nl:
        return "Python runtime"
    return n or "runtime"


def _is_model_file(path: str) -> bool:
    p = path.lower()
    return p.endswith((
        ".safetensors", ".ckpt", ".pt", ".pth", ".bin", ".onnx",
        ".gguf", ".tensors", ".engine",
    ))


def _kind_from_path(path: str) -> str:
    p = path.replace("\\", "/")
    if "/models/" in p:
        tail = p.split("/models/", 1)[1]
        seg = tail.split("/", 1)[0].strip().lower()
        if seg:
            return seg
    name = Path(path).name.lower()
    if "real" in name or "refiner" in name:
        return "upscale"
    if "hunyuan" in name or "video" in name:
        return "video"
    if "vae" in name:
        return "vae"
    if "clip" in name:
        return "clip"
    return "model"


def _component_kind_label(kind: str) -> str:
    k = (kind or "model").lower()
    labels = {
        "checkpoints": "Base checkpoint",
        "gpumodel": "Base checkpoint",
        "weight_models": "latent pipeline model",
        "unet": "UNet",
        "clip": "CLIP text",
        "clip_vision": "CLIP vision",
        "vae": "VAE",
        "loras": "LoRA",
        "guidance net": "guidance net",
        "upscale_models": "Upscaler",
        "upscale": "Upscaler",
        "video": "Video model",
        "conditioner": "Face adapter",
        "served-model": "Served model",
        "model": "Model",
    }
    return labels.get(k, labels["model"])


def _simple_component_name(kind: str, name: str) -> str:
    n = (name or "").strip()
    low = n.lower()
    if "refiner" in low:
        return "the refiner x4"
    if "conditioner" in low or "conditioner" in low:
        return "conditioner Face"
    if "clip-vit" in low or "vit-h" in low:
        return "CLIP ViT-H"
    if low.endswith(".safetensors"):
        n = n[:-12]
    elif low.endswith(".ckpt"):
        n = n[:-5]
    elif low.endswith(".pth"):
        n = n[:-4]
    return n or "Model"


def _proc_maps_readable(pid: int) -> bool:
    """Whether we can actually observe this process's memory maps. Decides if an
    empty component scan means "nothing loaded" (readable) or "unknown" (not)."""
    try:
        with open(f"/proc/{pid}/maps") as f:
            f.readline()
        return True
    except Exception:  # noqa: BLE001
        return False


def _read_mapped_model_components(pid: int) -> list[dict]:
    """Best-effort list of model files currently memory-mapped by process."""
    now = time.time()
    cached = _MAPS_CACHE.get(pid)
    if cached and (now - cached.get("ts", 0)) < 4.0:
        return list(cached.get("items") or [])

    out = []
    seen = set()
    try:
        with open(f"/proc/{pid}/maps", encoding="utf-8", errors="ignore") as f:
            for line in f:
                parts = line.strip().split()
                if len(parts) < 6:
                    continue
                path = parts[-1]
                if not path.startswith("/"):
                    continue
                if not _is_model_file(path):
                    continue
                # Focus on model roots to avoid unrelated binaries.
                low = path.lower()
                if "/gpu-service/models/" not in low and "/models/" not in low:
                    continue
                if path in seen:
                    continue
                seen.add(path)
                out.append({
                    "name": _simple_component_name(_kind_from_path(path), Path(path).name),
                    "kind": _kind_from_path(path),
                    "kind_label": _component_kind_label(_kind_from_path(path)),
                    "path": path,
                    "in_memory": True,
                })
    except Exception:  # noqa: BLE001
        out = []

    order = {
        "checkpoints": 1,
        "weight_models": 2,
        "unet": 3,
        "clip": 4,
        "clip_vision": 5,
        "vae": 6,
        "loras": 7,
        "guidance net": 8,
        "upscale_models": 9,
        "video": 10,
        "upscale": 11,
        "model": 50,
    }
    out.sort(key=lambda x: (order.get(x.get("kind", "model"), 99), x.get("name", "")))
    _MAPS_CACHE[pid] = {"ts": now, "items": out}
    return list(out)


def _read_open_model_components(pid: int) -> list[dict]:
    """Fallback: inspect open file handles for model files."""
    lsof = shutil.which("lsof")
    if not lsof:
        return []
    out = []
    seen = set()
    txt = _run([lsof, "-p", str(pid), "-Fn"], timeout=5)
    for line in txt.splitlines():
        if not line.startswith("n"):
            continue
        path = line[1:].strip()
        if not path.startswith("/"):
            continue
        if not _is_model_file(path):
            continue
        low = path.lower()
        if "/gpu-service/models/" not in low and "/models/" not in low:
            continue
        if path in seen:
            continue
        seen.add(path)
        out.append({
            "name": _simple_component_name(_kind_from_path(path), Path(path).name),
            "kind": _kind_from_path(path),
            "kind_label": _component_kind_label(_kind_from_path(path)),
            "path": path,
            "in_memory": True,
        })
    out.sort(key=lambda x: (x.get("kind", ""), x.get("name", "")))
    return out


def _attach_components(rows: list[dict]) -> list[dict]:
    out = []
    for r in rows:
        item = dict(r)
        comps = []
        pid = item.get("pid")
        runtime = str(item.get("name") or "").lower()
        source = str(item.get("source") or "").lower()
        model = str(item.get("model") or "")

        if isinstance(pid, int) and pid > 0 and ("gpusvc" in runtime or "python runtime" in runtime):
            comps = _read_mapped_model_components(pid)
            if not comps:
                comps = _read_open_model_components(pid)

        # Fallback: no model files observed in the process, so show the
        # CONFIGURED gpusvc stack — labeled truthfully, never as resident. If we
        # could read the memory maps, an empty scan means "really not loaded"
        # (gpusvc frees weights when idle); if visibility is restricted
        # (container boundary), residency is unknown (None), not asserted.
        if (not comps) and "gpusvc" in runtime:
            try:  # same resolution gpu_service uses (env -> ava.yaml -> the GPU model base)
                import gpu_service
                ckpt = gpu_service.DEFAULT_CKPT
            except Exception:  # noqa: BLE001
                ckpt = os.environ.get("AVA_GPU_MODEL", "gpu_model_base")
            upscaler = os.environ.get("AVA_UPSCALE_MODEL", "refiner_x4plus.pth")
            observable = isinstance(pid, int) and pid > 0 and _proc_maps_readable(pid)
            resident = False if observable else None
            comps = [
                {
                    "name": _simple_component_name("checkpoints", ckpt),
                    "kind": "checkpoints",
                    "kind_label": _component_kind_label("checkpoints"),
                    "path": None,
                    "in_memory": resident,
                },
                {
                    "name": _simple_component_name("upscale_models", upscaler),
                    "kind": "upscale_models",
                    "kind_label": _component_kind_label("upscale_models"),
                    "path": None,
                    "in_memory": resident,
                },
            ]

        if (not comps) and model and (source == "api" or item.get("backend")):
            comps = [{
                "name": _simple_component_name("served-model",
                                               str(item.get("model_id") or model)),
                "kind": "served-model",
                "kind_label": _component_kind_label("served-model"),
                "path": None,
                "in_memory": item.get("status") == "loaded",
            }]

        item["components"] = comps
        item["component_count"] = sum(1 for c in comps if c.get("in_memory"))
        item["in_memory"] = (item.get("status") == "loaded")
        gu = item.get("gpu_util")
        item["gpu_active"] = bool(gu is not None and gu > 0)
        out.append(item)
    return out


def _run(cmd: list[str], timeout: int = 4) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return (r.stdout or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _gpu() -> dict:
    """Primary accelerator telemetry via the HAL, in the monitor's dict shape.

    NVIDIA (NVML/nvidia-smi) gives util/temp/power/VRAM; Apple Silicon gives the
    unified-memory figure with util/temp/power None (no unprivileged API); a
    CPU-only box gives an all-None dict. Memory is reported in MiB to preserve
    the existing UI contract.
    """
    g = hwinfo.gpu()
    return {
        "name": g.name,
        "util": g.util,
        "temp": g.temp_c,
        "power": g.power_w,
        "mem_used_mb": round(g.mem_used_gb * 1024) if g.mem_used_gb is not None else None,
        "mem_total_mb": round(g.mem_total_gb * 1024) if g.mem_total_gb is not None else None,
    }


def _gpu_process_util() -> dict[int, float]:
    """Per-process GPU SM utilization from `nvidia-smi pmon` (best effort)."""
    smi = shutil.which("nvidia-smi")
    if not smi:
        return {}
    txt = _run([smi, "pmon", "-c", "1", "-s", "u"], timeout=4)
    out: dict[int, float] = {}
    for line in txt.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        # expected columns: gpu pid type sm mem enc dec command
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[1])
        except Exception:  # noqa: BLE001
            continue
        sm = _num(parts[3])
        if sm is None:
            continue
        out[pid] = max(out.get(pid, 0.0), float(sm))
    return out


def _extract_model_names(cmdline: str) -> list[str]:
    """Every model id named on a command line (--model, --served-model-name, …)."""
    if not cmdline:
        return []
    pats = [
        r"--model[= ]([^\s]+)",
        r"--served-model-name[= ]([^\s]+)",
        r"--ckpt_name[= ]([^\s]+)",
    ]
    out: list[str] = []
    for p in pats:
        for m in re.finditer(p, cmdline):
            v = m.group(1).strip('"\'')
            if v and v not in out:
                out.append(v)
    return out


def _extract_model(cmdline: str) -> str | None:
    names = _extract_model_names(cmdline)
    return names[0] if names else None


def _proc_cmdline(pid: int) -> str:
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            raw = f.read().replace(b"\x00", b" ").strip()
        return raw.decode("utf-8", errors="ignore")
    except Exception:  # noqa: BLE001
        return ""


def _proc_ppid(pid: int) -> int | None:
    try:
        with open(f"/proc/{pid}/status") as f:
            for line in f:
                if line.startswith("PPid:"):
                    return int(line.split()[1]) or None
    except Exception:  # noqa: BLE001
        return None
    return None


def _resolve_owner(pid: int, cmd: str) -> tuple[int, str, str | None]:
    """(owner_pid, owner_cmdline, model_id) for a GPU compute process.

    Engine workers (vLLM's EngineCore, forked dataloaders, …) run with bare
    cmdlines; the launcher that declared `--model …` is an ancestor. Walking up
    the tree lets sibling workers of one engine collapse into a single entry
    and names the model from what was actually launched — never a guess.
    """
    model = _extract_model(cmd)
    if model:
        return pid, cmd, model
    cur = pid
    for _ in range(6):
        parent = _proc_ppid(cur)
        if not parent or parent == 1:
            break
        pcmd = _proc_cmdline(parent)
        model = _extract_model(pcmd)
        if model:
            return parent, pcmd, model
        cur = parent
    return pid, cmd, None


def _parse_mem_mb(s: str | None) -> float | None:
    if not s:
        return None
    m = re.match(r"\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]i?)?B?\s*$", s)
    if not m:
        return None
    v = float(m.group(1))
    unit = (m.group(2) or "M").lower()
    factors = {
        "k": 1 / 1024,
        "ki": 1 / 1024,
        "m": 1,
        "mi": 1,
        "g": 1024,
        "gi": 1024,
        "t": 1024 * 1024,
        "ti": 1024 * 1024,
        "p": 1024 * 1024 * 1024,
        "pi": 1024 * 1024 * 1024,
    }
    return v * factors.get(unit, 1)


def _gpu_model_processes() -> list[dict]:
    """Active GPU compute processes, grouped per engine, with inferred model.

    One inference engine usually appears as several GPU processes (a launcher
    plus workers it forks). Rows are grouped by the owning process that named
    the model on its command line, so an engine is one entry no matter how
    many workers it spawns, and the model id is read — never assumed.
    """
    smi = shutil.which("nvidia-smi")
    if not smi:
        return []
    out = _run([
        smi,
        "--query-compute-apps=pid,process_name,used_memory",
        "--format=csv,noheader,nounits",
    ], timeout=4)
    util_by_pid = _gpu_process_util()
    groups: dict[int, dict] = {}
    for line in out.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        try:
            pid = int(parts[0])
        except Exception:  # noqa: BLE001
            continue
        proc = parts[1] or "process"
        mem_mb = _num(parts[2])
        cmd = _proc_cmdline(pid)
        owner, owner_cmd, model = _resolve_owner(pid, cmd)
        g = groups.setdefault(owner, {
            "model_id": None, "owner_cmd": "", "mem_mb": None, "util": None,
            "top_pid": pid, "top_mem": -1.0, "ctx": [],
        })
        if model and not g["model_id"]:
            g["model_id"], g["owner_cmd"] = model, owner_cmd
        if mem_mb is not None:
            g["mem_mb"] = (g["mem_mb"] or 0.0) + mem_mb
        u = util_by_pid.get(pid)
        if u is not None:
            g["util"] = max(g["util"] or 0.0, u)
        if (mem_mb or 0.0) > g["top_mem"]:
            g["top_mem"], g["top_pid"] = (mem_mb or 0.0), pid
        g["ctx"].append(f"{proc} {cmd}".strip())
    rows = []
    for owner, g in groups.items():
        runtime_ctx = " ".join(filter(None, [g["owner_cmd"], *g["ctx"]]))
        model = g["model_id"]
        mem_mb = g["mem_mb"]
        rows.append({
            "id": f"pid:{owner}",
            "name": _short_runtime_name(runtime_ctx, model=model),
            "model": _short_model_name(model, runtime=runtime_ctx),
            "model_id": model,
            "memory_mb": mem_mb,
            "memory_gb": round(mem_mb / 1024, 2) if mem_mb is not None else None,
            "gpu_util": g["util"],
            # Surface the worker actually holding the weights (largest member).
            "pid": g["top_pid"],
            "status": "loaded",
            "source": "nvidia-smi",
            "cmd": (g["owner_cmd"] or (g["ctx"][0] if g["ctx"] else ""))[:300],
        })
    rows.sort(key=lambda x: x.get("memory_mb") or 0, reverse=True)
    return rows


def _docker_model_containers() -> list[dict]:
    """Fallback model inventory from running docker containers."""
    docker = shutil.which("docker")
    if not docker:
        return []
    names_out = _run([docker, "ps", "--format", "{{.Names}}"], timeout=4)
    names = [n.strip() for n in names_out.splitlines() if n.strip()]
    if not names:
        return []

    stats_out = _run([docker, "stats", "--no-stream", "--format", "{{.Name}}|{{.MemUsage}}"], timeout=6)
    mem_by_name = {}
    for line in stats_out.splitlines():
        if "|" not in line:
            continue
        n, usage = line.split("|", 1)
        used = usage.split("/")[0].strip()
        mem_by_name[n.strip()] = _parse_mem_mb(used)

    out = []
    for n in names:
        cmd_json = _run([docker, "inspect", "--format", "{{json .Config.Cmd}}", n], timeout=4)
        pid_txt = _run([docker, "inspect", "--format", "{{.State.Pid}}", n], timeout=4)
        cmdline = ""
        pid = None
        if cmd_json:
            try:
                arr = json.loads(cmd_json)
                if isinstance(arr, list):
                    cmdline = " ".join(str(x) for x in arr)
                else:
                    cmdline = str(arr)
            except Exception:  # noqa: BLE001
                cmdline = cmd_json
        try:
            p = int((pid_txt or "").strip())
            pid = p if p > 0 else None
        except Exception:  # noqa: BLE001
            pid = None
        model = _extract_model(cmdline)
        lower = n.lower()
        if not model and not any(k in lower for k in ("vllm", "gpusvc", "hunyuan", "ollama")):
            continue
        mem_mb = mem_by_name.get(n)
        runtime_name = _short_runtime_name(n, model=model)
        short_model = _short_model_name(model, runtime=n)
        out.append({
            "id": f"ctr:{n}",
            "name": runtime_name,
            "model": short_model,
            "model_id": model,
            "memory_mb": mem_mb,
            "memory_gb": round(mem_mb / 1024, 2) if mem_mb is not None else None,
            "gpu_util": None,
            "pid": pid,
            "status": "loaded",
            "source": "docker",
            "cmd": cmdline[:300],
        })
    out.sort(key=lambda x: x.get("memory_mb") or 0, reverse=True)
    return out


def _configured_backends() -> list[dict]:
    """Local inference backends Ava is configured to use.

    Read from the router's registry (ava.yaml `inference.backends` → env →
    built-in default), so this module never hardcodes a model identity — a
    fork that reconfigures its backends reconfigures this monitor with them.
    Cloud endpoints are excluded: the monitor reports THIS box's memory.
    """
    try:  # lazy import: keeps this module importable standalone
        from . import router_app
        backends = router_app.load_backends()
    except Exception:  # noqa: BLE001 — telemetry must not depend on the router
        return []
    local = []
    for b in backends:
        host = urlparse(str(b.get("url") or "")).hostname or ""
        if host in {"127.0.0.1", "localhost", "::1", "0.0.0.0"}:
            local.append(b)
    return local


def _match_backend_row(rows: list[dict], served_ids: list[str]) -> dict | None:
    """The process/container row already representing one of these model ids."""
    cands = set()
    for s in served_ids:
        s = (s or "").strip().lower()
        if s:
            cands.add(s)
            cands.add(s.rsplit("/", 1)[-1])
    if not cands:
        return None
    for r in rows:
        names = _extract_model_names(r.get("cmd") or "")
        if r.get("model_id"):
            names.append(str(r["model_id"]))
        for n in names:
            n = n.lower()
            if n in cands or n.rsplit("/", 1)[-1] in cands:
                return r
    return None


def _loaded_models() -> list[dict]:
    procs = _gpu_model_processes()
    rows = procs if procs else _docker_model_containers()

    # Cross-check each configured local backend's /v1/models: the row serving
    # it gets tagged (and takes the config's display label), engines invisible
    # to process telemetry still get a row, and a configured-but-unreachable
    # engine is surfaced as offline. Identity comes from config or the live
    # API — never from guessing what a GPU process "probably" is.
    for b in _configured_backends():
        # One parser for "what is this engine serving", shared with the setup
        # wizard. This hardcoded `/models` and the OpenAI `data[].id` shape, so
        # an Ollama backend answered through its /v1 compatibility layer by luck
        # and a llama.cpp one was asked at whichever path happened to be right.
        from . import models as _models
        reachable, served = _models.probe_serving(
            str(b.get("url", "")), str(b.get("engine", "")), timeout=1.5)

        label = str(b.get("label") or "").strip()
        row = _match_backend_row(rows, served or [str(b.get("model") or "")])
        if row is None and served:
            # Engine is up but no process exposed a model id (bare workers on a
            # platform without /proc, container boundaries, …): claim the one
            # unnamed row of the same engine kind rather than duplicating it.
            engine = str(b.get("engine") or "").lower()
            row = next((x for x in rows
                        if not x.get("model_id")
                        and engine and engine in str(x.get("name", "")).lower()), None)
            if row is not None:
                row["model_id"] = served[0]
                row["model"] = _short_model_name(served[0], runtime=row.get("name"))

        if row is not None:
            row["backend"] = b.get("id")
            if label:
                row["model"] = label
            continue

        if served:
            for mid in served:
                rows.append({
                    "id": f"{b.get('id')}:{mid}",
                    "name": _short_runtime_name(str(b.get("engine") or ""), model=mid),
                    "model": label if label and len(served) == 1 else _short_model_name(mid),
                    "model_id": mid,
                    "memory_mb": None, "memory_gb": None, "gpu_util": None, "pid": None,
                    "status": "loaded", "source": "api", "cmd": "",
                    "backend": b.get("id"),
                })
        else:
            rows.append({
                "id": f"{b.get('id')}:offline",
                "name": _short_runtime_name(str(b.get("engine") or "")),
                "model": label or _short_model_name(str(b.get("model") or ""),
                                                    runtime=str(b.get("engine") or "")),
                "model_id": b.get("model") or None,
                "memory_mb": None, "memory_gb": None, "gpu_util": None, "pid": None,
                "status": "empty" if reachable else "offline", "source": "api", "cmd": "",
                "backend": b.get("id"),
            })

    rows.sort(key=lambda x: x.get("memory_mb") or 0, reverse=True)
    return _attach_components(rows)


def _mem() -> dict:
    """System (unified) memory pool via the HAL — cross-platform."""
    m = hwinfo.system_mem()
    if not m.readable or not m.total_gb:
        return {"total_gb": None, "used_gb": None, "free_gb": None, "used_pct": None}
    used = m.total_gb - m.free_gb
    return {"total_gb": round(m.total_gb, 1), "used_gb": round(used, 1),
            "free_gb": round(m.free_gb, 1),
            "used_pct": round(100 * used / m.total_gb)}


def _cpu(interval: float = 0.2):
    """CPU utilisation %. psutil (cross-platform) with a /proc/stat fallback."""
    if _psutil is not None:
        try:
            return round(_psutil.cpu_percent(interval=interval))
        except Exception:  # noqa: BLE001
            pass

    def read():
        with open("/proc/stat") as f:
            parts = f.readline().split()[1:]
        vals = [int(x) for x in parts]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0)  # idle + iowait
        return idle, sum(vals)
    try:
        i1, t1 = read()
        time.sleep(interval)
        i2, t2 = read()
        dt = t2 - t1
        di = i2 - i1
        return round(100 * (dt - di) / dt) if dt else None
    except Exception:  # noqa: BLE001
        return None


def _disk(path: str = "/") -> dict:
    """Root filesystem (the SSD) capacity: total / used / free."""
    try:
        u = shutil.disk_usage(path)
    except Exception:  # noqa: BLE001
        return {"total_gb": None, "used_gb": None, "free_gb": None, "used_pct": None}
    g = 1024 ** 3
    return {"total_gb": round(u.total / g, 1), "used_gb": round(u.used / g, 1),
            "free_gb": round(u.free / g, 1),
            "used_pct": round(100 * u.used / u.total) if u.total else None}


def _model_role(model, backend_id: str | None = None) -> str:
    """What a loaded model is FOR, so a GPU spike on it is self-explanatory.

    A row tagged with a configured inference backend IS Ava's brain — that
    comes from config, not from pattern-matching the model's name.
    """
    if backend_id:
        return "Ava's brain (chat inference)"
    m = (model or "").lower()
    if "gpusvc" in m:
        return "Image & video rendering"
    if "hunyuan" in m or "wan" in m:
        return "Video rendering"
    if "gpumodel" in m or "flux" in m:
        return "Image rendering"
    # Connector-declared hints (model_hints: in a connector.yaml).
    try:
        from . import connectors
        for h in connectors.model_hints():
            if any(s in m for s in h["match"]):
                return h["role"]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _active_jobs() -> list[dict]:
    """Currently-running jobs across the box so a GPU spike can be attributed to a
    named task (render / video / chat image) instead of a bare percentage.

    External jobs come from connectors that declare a `jobs:` block (see
    connectors.job_sources) — the registry is cached (~30 s), so a fork with no
    such connectors makes zero HTTP calls here.
    """
    jobs: list[dict] = []
    try:  # lazy import: keeps this module importable standalone
        from . import connectors
        sources = connectors.job_sources()
    except Exception:  # noqa: BLE001
        sources = []
    for src in sources:
        try:
            r = requests.get(src["url"], params=src["params"], timeout=1.5)
            if r.ok:
                for j in r.json().get(src["list_key"], []):
                    kind = j.get("kind")
                    # Just the job/service type — no free text (the monitor is
                    # about what's running on the box, not the content).
                    jobs.append({
                        "name": src["labels"].get(kind or "",
                                                  str(kind).title() if kind else "Job"),
                        "stage": j.get("stage"),
                        "progress": j.get("progress"),
                        "engine": src.get("engine"),
                    })
        except Exception:  # noqa: BLE001 — best-effort; the panel degrades gracefully
            pass
    # Ava's own chat image renders (this process's media-job tracker).
    try:
        from . import state
        with state.jobs_lock:
            running = [dict(j) for j in state.jobs.values() if j.get("status") == "running"]
        for j in running:
            jobs.append({
                "name": "Chat image render",
                "stage": j.get("stage"),
                "progress": j.get("progress"),
                "engine": "the GPU service",
            })
    except Exception:  # noqa: BLE001
        pass
    return jobs


def stats() -> dict:
    """One live snapshot of the device's hardware."""
    models = _loaded_models()
    for m in models:
        m["role"] = _model_role(m.get("model_id") or m.get("model"), m.get("backend"))
    return {"gpu": _gpu(), "mem": _mem(), "disk": _disk(),
            "cpu": {"util": _cpu()}, "models": models,
            "jobs": _active_jobs(),
            "ts": time.time()}


# --- lightweight time-series sampler (ring buffer) for the dashboard ---------
# A background thread records a compact hardware sample every few seconds so the
# dashboard can chart GPU util/temp/power/mem/CPU over time (the snapshot above
# is a single point). Kept in-process (no DB) — cheap and plenty for one host.
_SAMPLE_INTERVAL = float(os.environ.get("AVA_HW_SAMPLE_INTERVAL", "5"))
_SAMPLE_KEEP_S = float(os.environ.get("AVA_HW_SAMPLE_KEEP_S", str(2 * 3600)))
_HISTORY: deque = deque(maxlen=int(_SAMPLE_KEEP_S / max(1.0, _SAMPLE_INTERVAL)) + 8)
_HISTORY_LOCK = threading.Lock()
_SAMPLER_STARTED = False


def _sample_once() -> dict:
    g = _gpu()
    m = _mem()
    return {
        "ts": round(time.time(), 1),
        "gpu_util": g.get("util"),
        "gpu_temp": g.get("temp"),
        "gpu_power": g.get("power"),
        "mem_used_pct": m.get("used_pct"),
        "mem_used_gb": m.get("used_gb"),
        "cpu": _cpu(),
    }


def _sampler_loop() -> None:
    while True:
        try:
            s = _sample_once()
            with _HISTORY_LOCK:
                _HISTORY.append(s)
        except Exception:  # noqa: BLE001 — never let the sampler die
            pass
        time.sleep(_SAMPLE_INTERVAL)


# --- persistent, downsampled history (day → 5-year dashboard ranges) ---------
# The ring buffer above only holds ~2h and dies on restart, so it can't back the
# Week/Month/Year/5-Year filters. A tiny background thread rolls the hot buffer
# into two bounded on-disk tiers (1-minute rows kept 90d, 1-hour rows kept 5y).
# `history_series` then stitches the right tier + the hot tail and averages into
# whatever bucket the chart asks for. Compact JSONL, no DB — same "cheap, one
# host" spirit as the sampler. Everything is wrapped so a disk error can never
# take down sampling.
_HW_FIELDS = ("gpu_util", "gpu_temp", "gpu_power", "mem_used_pct", "mem_used_gb", "cpu")
# (filename, cadence seconds). Retention per tier is resolved live from the
# user's data.retention_days setting via _tier_retention_s (below).
_TIERS = (
    ("hw_1m.jsonl", 60),
    ("hw_1h.jsonl", 3600),
)
_MINUTE_TIER_CAP_S = 90 * 86400  # minute-resolution never kept beyond 90d (file size)
_PERSIST_STARTED = False


def _retention_s() -> float:
    """User-configured retention in seconds (0 == forever). Setup → System."""
    try:
        from . import settings
        return settings.data_retention_s()
    except Exception:  # noqa: BLE001
        return 183 * 86400


def _tier_retention_s(name: str) -> float:
    """How long a given tier's rows are kept. The minute tier is additionally
    capped at 90d regardless of the setting (long ranges use the hour tier)."""
    full = _retention_s()  # 0 == forever
    if name == "hw_1m.jsonl":
        return _MINUTE_TIER_CAP_S if full <= 0 else min(full, _MINUTE_TIER_CAP_S)
    return full


def _dur_s(s, default: float) -> float:
    """Parse a '5m' / '6h' / '30d' duration to seconds (mirrors dashboard._parse_dur)."""
    try:
        mult = {"m": 60, "h": 3600, "d": 86400}.get(str(s)[-1].lower())
        return float(str(s)[:-1]) * mult if mult else default
    except Exception:  # noqa: BLE001
        return default


def _hw_dir() -> str:
    try:
        from . import settings
        base = settings.logs_dir()
    except Exception:  # noqa: BLE001
        base = os.environ.get("AVA_LOGS_DIR") or os.path.expanduser("~/.ava/logs")
    return os.path.join(base, "hw_history")


def _p(name: str) -> str:
    return os.path.join(_hw_dir(), name)


def _avg_samples(rows: list) -> dict:
    """Average each numeric field across rows, ignoring missing values."""
    out: dict = {}
    for f in _HW_FIELDS:
        vals = [r[f] for r in rows if isinstance(r.get(f), (int, float))]
        out[f] = round(sum(vals) / len(vals), 2) if vals else None
    return out


def _append_jsonl(path: str, row: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(row, separators=(",", ":")) + "\n")


def _read_jsonl_since(path: str, cutoff: float) -> list:
    rows: list = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    r = json.loads(line)
                except Exception:  # noqa: BLE001 — skip a torn line
                    continue
                if (r.get("ts") or 0) >= cutoff:
                    rows.append(r)
    except FileNotFoundError:
        pass
    return rows


def _last_row_ts(path: str) -> float:
    try:
        last = 0.0
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        last = float(json.loads(line).get("ts") or 0)
                    except Exception:  # noqa: BLE001
                        pass
        return last
    except FileNotFoundError:
        return 0.0


def _prune_jsonl(path: str, retention_s: float, now: float) -> None:
    """Drop rows older than the retention window (rewrite in place).
    retention_s <= 0 means keep forever — nothing to prune."""
    if retention_s <= 0:
        return
    cutoff = now - retention_s
    kept = _read_jsonl_since(path, cutoff)
    try:
        tmp = path + ".tmp"
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            for r in kept:
                f.write(json.dumps(r, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    except FileNotFoundError:
        pass
    except Exception:  # noqa: BLE001
        pass


def _persist_loop() -> None:
    # Seed each tier's clock from what's already on disk so restarts don't
    # double-write a bucket.
    last_flush = {interval: _last_row_ts(_p(name)) for name, interval in _TIERS}
    last_prune = 0.0
    while True:
        try:
            now = time.time()
            with _HISTORY_LOCK:
                snap = list(_HISTORY)
            for name, interval in _TIERS:
                if now - last_flush[interval] < interval:
                    continue
                grp = [r for r in snap if (r.get("ts") or 0) >= now - interval]
                if grp:
                    row = _avg_samples(grp)
                    row["ts"] = round(now, 1)
                    _append_jsonl(_p(name), row)
                last_flush[interval] = now
            if now - last_prune >= 3600:
                for name, _interval in _TIERS:
                    _prune_jsonl(_p(name), _tier_retention_s(name), now)
                last_prune = now
        except Exception:  # noqa: BLE001 — persistence must never break sampling
            pass
        time.sleep(30)


def history_series(since: str = "1d", bucket: str = "5m", max_points: int = 720) -> list[dict]:
    """Bucket-averaged hardware samples for a dashboard range. Reads the tier that
    covers the window (1-min ≤90d, else 1-hour) plus the hot ring-buffer tail,
    then averages into `bucket`-wide points. Returns oldest → newest."""
    now = time.time()
    span = _dur_s(since, 86400)
    step = max(1.0, _dur_s(bucket, 300))
    cutoff = now - span
    name = "hw_1m.jsonl" if span <= 90 * 86400 else "hw_1h.jsonl"
    rows = _read_jsonl_since(_p(name), cutoff)
    with _HISTORY_LOCK:
        rows += [r for r in _HISTORY if (r.get("ts") or 0) >= cutoff]
    buckets: dict = {}
    for r in rows:
        ts = r.get("ts") or 0
        if ts < cutoff:
            continue
        bts = int(ts // step * step)
        buckets.setdefault(bts, []).append(r)
    out = []
    for bts in sorted(buckets):
        row = _avg_samples(buckets[bts])
        row["ts"] = bts
        out.append(row)
    return out[-max_points:]


def start_sampler() -> None:
    """Start the background hardware sampler + persistence once (idempotent)."""
    global _SAMPLER_STARTED, _PERSIST_STARTED
    if _SAMPLER_STARTED:
        return
    _SAMPLER_STARTED = True
    threading.Thread(target=_sampler_loop, daemon=True).start()
    if not _PERSIST_STARTED:
        _PERSIST_STARTED = True
        threading.Thread(target=_persist_loop, daemon=True).start()


def history(since: float | None = None, limit: int = 2000) -> list[dict]:
    """Recorded hardware samples (oldest -> newest), optionally since a ts."""
    with _HISTORY_LOCK:
        rows = list(_HISTORY)
    if since is not None:
        rows = [r for r in rows if r.get("ts", 0) >= since]
    return rows[-limit:]


def latest_sample() -> dict | None:
    """Most recent hardware sample (for alert evaluation), or None."""
    with _HISTORY_LOCK:
        return dict(_HISTORY[-1]) if _HISTORY else None
