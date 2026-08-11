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
        if any(k in rl or k in ml for k in ("vllm", "enginecore")):
            return "vLLM model"
        return "Model"

    # HF-style org/name ids and file paths: the tail segment is the name.
    m = m.replace("\\", "/").split("/")[-1]
    if _is_model_file(m):
        m = m.rsplit(".", 1)[0]
    return m


def _short_runtime_name(name: str | None) -> str:
    n = (name or "").strip()
    nl = n.lower()

    if "vllm" in nl or "enginecore" in nl:
        return "vLLM"
    if "ollama" in nl:
        return "Ollama"
    if "llama-server" in nl or "llamacpp" in nl or "llama.cpp" in nl:
        return "llama.cpp"
    if "mlx" in nl:
        return "MLX"
    if "lmstudio" in nl or "lm-studio" in nl or "lm studio" in nl:
        return "LM Studio"
    if "python" in nl:
        return "Python runtime"
    # The fallback is a concatenated cmdline when it comes from process
    # telemetry, and the UI prints it as one "Runtime:" line — so cap it
    # rather than spilling kilobytes of flags across the panel.
    return (n[:60].strip() or "runtime")


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
        "unet": "UNet",
        "clip": "CLIP text",
        "clip_vision": "CLIP vision",
        "vae": "VAE",
        "loras": "LoRA",
        "served-model": "Served model",
        "model": "Model",
    }
    return labels.get(k, labels["model"])


def _simple_component_name(kind: str, name: str) -> str:
    n = (name or "").strip()
    low = n.lower()
    if "clip-vit" in low or "vit-h" in low:
        return "CLIP ViT-H"
    if low.endswith(".safetensors"):
        n = n[:-12]
    elif low.endswith(".ckpt"):
        n = n[:-5]
    elif low.endswith(".pth"):
        n = n[:-4]
    return n or "Model"


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
                if "/models/" not in low:
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
        "unet": 2,
        "clip": 3,
        "clip_vision": 4,
        "vae": 5,
        "loras": 6,
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
        if "/models/" not in low:
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

        if isinstance(pid, int) and pid > 0 and "python runtime" in runtime:
            comps = _read_mapped_model_components(pid)
            if not comps:
                comps = _read_open_model_components(pid)

        # Residency is tri-state everywhere in this module: observed resident,
        # observed NOT resident, or unobservable. A row whose state we could
        # not read must not answer "no" — that is the same lie as reporting a
        # model Ava cannot see as missing.
        state = str(item.get("state") or
                    ("resident" if item.get("status") == "loaded" else "unknown"))
        resident = (True if state == "resident"
                    else None if state in ("unknown", "remote") else False)

        if (not comps) and model and (source in ("api", "agent") or item.get("backend")):
            comps = [{
                "name": _simple_component_name("served-model",
                                               str(item.get("model_id") or model)),
                "kind": "served-model",
                "kind_label": _component_kind_label("served-model"),
                "path": None,
                "in_memory": resident,
            }]

        item["components"] = comps
        item["component_count"] = sum(1 for c in comps if c.get("in_memory"))
        item["in_memory"] = resident
        gu = item.get("gpu_util")
        item["gpu_active"] = bool(gu is not None and gu > 0)
        out.append(item)
    return out


_ENTRYPOINTS = ("main.py", "app.py", "server.py", "run.py", "__main__.py", "cli.py")

# Directory names that describe a KIND of weight rather than the thing itself,
# so they never become a model's display name. `/models/diffusion/flux2/vae/…`
# is a `flux2`, not a "diffusion" and not a "vae".
_KIND_DIRS = {
    "models", "diffusion", "diffusion_models", "checkpoints", "unet", "vae",
    "clip", "clip_vision", "text_encoders", "loras", "lora", "controlnet",
    "embeddings", "upscale_models", "transformers", "safetensors", "gguf",
    "snapshots", "blobs", "weights", "hub",
}


def _app_from_cmdline(cmd: str) -> str:
    """The program a command line belongs to, or "".

    Read, not guessed: `/opt/ImageGen/main.py` names ImageGen, because that is
    the directory holding the entrypoint the process was started with. No table
    of known applications — a fork running anything gets the same treatment.
    """
    for tok in (cmd or "").split():
        norm = tok.replace("\\", "/")
        base = norm.rsplit("/", 1)[-1].lower()
        if base in _ENTRYPOINTS and "/" in norm:
            parent = norm.rsplit("/", 2)[-2]
            # A venv's own bin/ or a src/ wrapper says nothing about the app.
            if parent.lower() not in {"bin", "src", "app", ".venv", "venv"}:
                return parent
    return ""


def _family_from_components(comps: list[dict]) -> str:
    """The folder the owner filed these weights under, or "".

    The deepest path segment every component shares below a kind directory —
    for weights split across `vae/`, `diffusion_models/` and `text_encoders/`,
    that is the one folder all three sit under.

    Structural on purpose. Turning a filename like `foo2_dev_fp8mixed` into a
    branded "Foo 2.0" would be pattern-matching a model identity, which
    `_short_model_name` exists to refuse; a directory the owner made is a fact
    on disk.
    """
    segs: list[list[str]] = []
    for c in comps:
        p = str(c.get("path") or "").replace("\\", "/")
        if not p:
            continue
        parts = [s for s in p.split("/")[:-1] if s]
        segs.append(parts)
    if not segs:
        return ""
    common = segs[0]
    for s in segs[1:]:
        keep = 0
        for a, b in zip(common, s):
            if a != b:
                break
            keep += 1
        common = common[:keep]
    # Never climb above the store root. Without this, weights filed only under
    # kind directories (`…/models/diffusion/vae/`) walked past `models` and
    # named the row after whatever happened to hold it — `srv`, or a username.
    if "models" in [s.lower() for s in common]:
        last = max(i for i, s in enumerate(common) if s.lower() == "models")
        common = common[last + 1:]
    for seg in reversed(common):
        if seg.lower() not in _KIND_DIRS and not seg.startswith("."):
            return seg
    return ""


def _owning_app(row: dict) -> str:
    """The connected app that declared this row as its own, or "".

    Matched against an `owns:` block in a connector manifest (connectors.owns),
    in the order the evidence is trustworthy: an exact container name, then a
    substring of the command line, then a prefix of a mapped weight file.

    Note the row's id carries the OWNER pid while `pid` carries the top-memory
    worker — for a vLLM those differ — but neither is used here: the command
    line is already grouped onto the owner, and it survives the container
    boundary because the host's /proc shows a container's argv.
    """
    try:
        from . import connectors
        claims = connectors.owns()
    except Exception:  # noqa: BLE001 — telemetry must not depend on connectors
        return ""
    if not claims:
        return ""

    rid = str(row.get("id") or "").lower()
    container = rid.split("ctr:", 1)[1] if rid.startswith("ctr:") else ""
    cmd = str(row.get("cmd") or "").lower()
    paths = [str(c.get("path") or "").lower()
             for c in (row.get("components") or []) if c.get("path")]

    for c in claims:
        if container and container in c["container"]:
            return c["id"]
    for c in claims:
        if cmd and any(s in cmd for s in c["cmdline"]):
            return c["id"]
    for c in claims:
        if any(p.startswith(s) for s in c["paths"] for p in paths):
            return c["id"]
    return ""


def _name_from_evidence(rows: list[dict]) -> list[dict]:
    """Name a row Ava could not identify, from what is already on it.

    `_short_model_name` falls back to the literal word "Model" when a process
    exposes no model id, and the panel printed that verbatim — the least
    informative string available, on the row holding the most memory. Yet the
    same row carries the command line that started it and the weight files it
    has mapped.

    Only touches rows whose `model_id` is falsy: where an identity WAS read,
    nothing here may overwrite it. And it sets `model` only, never `model_id` —
    a folder name is not an id any engine would accept, so `model_id is None`
    stays the honest "we never read an identity" signal the UI keys off.
    """
    for r in rows:
        if r.get("model_id"):
            continue
        app = _app_from_cmdline(str(r.get("cmd") or ""))
        family = _family_from_components(r.get("components") or [])
        name = " · ".join(x for x in (app, family) if x)
        if name:
            r["model"] = name
    return rows


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


# A content-addressed blob: `sha256-<hex>`, `sha256:<hex>`, or a bare long hex
# digest. Ollama launches its runner with `--model …/blobs/sha256-1eee6953…`,
# which names a FILE, not a model.
_BLOB_RE = re.compile(r"^(?:sha\d{3}[-:])?[0-9a-f]{16,}$", re.I)


def _is_blob_name(value: str) -> bool:
    """Is this a content hash rather than a model anyone could recognise?

    The distinction is load-bearing, not cosmetic. Ollama's runner names its
    weights by digest, so the row built from that command line could never match
    the `dolphin3:8b` its own API reports — and `_loaded_models` duly appended a
    SECOND row for a model it already had, with a different memory figure. One
    model, listed twice, in two vocabularies.

    Narrow on purpose: `--model /models/Llama-3.1-8B-Q4.gguf` is a path too, and
    that basename identifies the model perfectly well. Only an opaque digest
    carries no identity.
    """
    return bool(_BLOB_RE.match((value or "").rsplit("/", 1)[-1].strip()))


def _extract_model_names(cmdline: str) -> list[str]:
    """Every model id named on a command line (--model, --served-model-name, …).

    A content-addressed blob is not one of them — see `_is_blob_name`. Dropping it
    here rather than at the point of comparison is deliberate: it leaves the row
    with `model_id=None`, which is the shape `_loaded_models` already knows how to
    hand to the engine's own API, so the row ends up named by the thing that
    actually knows the name.
    """
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
            if v and v not in out and not _is_blob_name(v):
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
            g["model_id"] = model
        # Keep the owner's command line whether or not it named a model. It was
        # recorded only alongside a model id, so a process that exposes none —
        # ComfyUI, a bare worker — kept nothing, and the one string that says
        # WHICH program this is survived only by luck via `ctx`. It is what
        # `_app_from_cmdline` reads to name the row.
        if owner_cmd and not g["owner_cmd"]:
            g["owner_cmd"] = owner_cmd
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
            "name": _short_runtime_name(runtime_ctx),
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
        if not model and not any(k in lower for k in ("vllm", "hunyuan", "ollama")):
            continue
        mem_mb = mem_by_name.get(n)
        runtime_name = _short_runtime_name(n)
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
    """Every inference backend Ava is configured to use, tagged local or not.

    Read from the router's registry (ava.yaml `inference.backends` → env →
    built-in default), so this module never hardcodes a model identity — a
    fork that reconfigures its backends reconfigures this monitor with them.

    This used to keep only four loopback hostname literals, which was the wrong
    test for the right idea. The monitor does report THIS box, but a Docker
    install's brain lives at `http://ollama:11434/v1` — a compose service name
    on the same host — and was discarded as if it were a cloud endpoint. The
    monitor then had nothing left to probe and told a user whose brain was
    running fine "No model process detected yet". Locality is now decided once,
    by engine kind and key (model_fit.is_local), and carried on the row as a
    fact instead of silently dropping it.
    """
    try:  # lazy import: keeps this module importable standalone
        from . import router_app
        backends = router_app.load_backends()
    except Exception:  # noqa: BLE001 — telemetry must not depend on the router
        return []
    from .model_fit import is_local
    out = []
    for b in backends:
        b = dict(b)
        try:
            b["local"] = is_local(b)
        except Exception:  # noqa: BLE001
            b["local"] = True
        out.append(b)
    return out


def _match_backend_row(rows: list[dict], served_ids: list[str]) -> dict | None:
    """The process/container row already representing one of these model ids.

    A row that already carries a backend is skipped. Two backends declared
    against one engine (the documented brain + fast layout, both on the same
    Ollama) are served the SAME model list, so the second one's ids matched the
    row the first had just claimed and took it over — leaving one row whose id
    came from one backend and whose label came from the other, and no row at all
    for the brain to be tagged on. That is the original "Ava's brain shows
    empty" symptom, reached by a second route.
    """
    cands = set()
    for s in served_ids:
        s = (s or "").strip().lower()
        if s:
            cands.add(s)
            cands.add(s.rsplit("/", 1)[-1])
    if not cands:
        return None
    for r in rows:
        if r.get("backend"):
            continue
        names = _extract_model_names(r.get("cmd") or "")
        if r.get("model_id"):
            names.append(str(r["model_id"]))
        for n in names:
            n = n.lower()
            if n in cands or n.rsplit("/", 1)[-1] in cands:
                return r
    return None


# What a row's `state` can be. A closed vocabulary, because the UI renders one
# sentence per value and "some other string" has no sentence.
#   resident  observed to be holding weights in memory right now
#   idle      the engine is up and has this model, but it is not in memory
#             (Ollama evicts after ~5 min; the next turn loads it again)
#   absent    the engine is up and does NOT have this model — configured but
#             never pulled, the single most common broken first install
#   offline   the engine could not be reached at all
#   remote    it runs somewhere else (cloud, another box), so this host's
#             memory is not where to look for it
#   unknown   we genuinely cannot observe residency (an engine that will not
#             say, a sandbox we cannot see into). Never a synonym for "no".
_STATES = ("resident", "idle", "absent", "offline", "remote", "unknown")

# WHOSE a row is. A closed vocabulary for the same reason `_STATES` is one: the
# UI renders one heading per value, and "some other string" has no heading.
#   brain       what Ava thinks with. Read from `role_key`, which only
#               models.effective_brain() sets — never re-derived here.
#   configured  an inference engine the owner set up. Ava can route to it.
#   app         it belongs to a CONNECTED APP, which declared it in an `owns:`
#               block. Ava can see it; Ava does not think with it.
#   foreign     other software on this box. Measured, not managed.
#
# This exists because the panel listed all four identically, so a third-party
# ComfyUI holding 65 GB and a never-configured phantom backend both read as
# "Ava's models" — which is what made a correct, live list look like stale junk.
_RELATIONS = ("brain", "configured", "app", "foreign")


def _backend_state(b: dict, reachable: bool, served: list[str],
                   resident: list[dict] | None) -> tuple[str, str, dict | None, bool]:
    """(state, confirmed_model_id, resident_entry, measured) for one backend.

    `measured` qualifies the RESIDENT claim, and only that claim. True when the
    answer came from reading the engine's residency endpoint or from GPU process
    telemetry; False when it was derived from the engine's nature — see the
    `serves_resident` branch below, which is the one place this function reaches
    a conclusion it did not read. Both render as "In memory", which is correct,
    but only one of them was observed, and the UI has to be able to say which.
    Same distinction `AllocModel.measured` already draws for resident_gib.

    Residency is OBSERVED here, never inferred from the existence of config.
    The distinction is the whole point: `/api/tags` lists what Ollama has on
    disk, so reading it as "loaded" reported three resident brains on a box
    holding none. An engine that loads one model at boot and holds it
    (`serves_resident`) is the one case where being served IS being resident.

    The middle value is "" unless the engine CONFIRMED it holds this model —
    callers use it to decide whether the configured display label may be
    trusted, so a hopeful "the config says so" must never be returned here.
    """
    from . import models as _models
    from .engines import get as _engine_get

    want = str(b.get("model") or "").strip()
    # Locality first: an engine that is not on this box is "somewhere else"
    # whether or not it answers, and we do not probe it at all (see the caller).
    # Testing reachability first made "remote" unreachable for anything down.
    if not b.get("local", True):
        return "remote", "", None, True
    if not reachable:
        return "offline", "", None, True

    spec = _engine_get(str(b.get("engine") or ""))

    # Which id this engine will actually accept for the configured model.
    matched = _models.match_served(want, served) if want else ""
    if not matched and not want and len(served) == 1:
        matched = served[0]
    if want and not matched:
        # It answered and it does not have this model. An EMPTY inventory says
        # that just as loudly as a non-empty one for an engine whose list is a
        # disk inventory (Ollama, LM Studio) — a fresh install that pulled
        # nothing is the commonest broken first run there is, and requiring a
        # non-empty list to say "absent" reported it as "Ready, not loaded: the
        # engine has it", which is the one thing it does not. An engine that
        # loads at boot and lists nothing is still starting, not empty, so it
        # falls through to the honest "unknown" below.
        if served or not getattr(spec, "serves_resident", True):
            return "absent", "", None, True
    if resident is None:
        if getattr(spec, "serves_resident", True) and (matched or served):
            # DERIVED, not read: this engine loads one model at boot and holds
            # it, so being served IS being resident. True of vLLM, llama.cpp and
            # MLX, which expose no residency endpoint to read.
            return "resident", matched, None, False
        return "unknown", matched, None, True

    ids = [str(r.get("id") or "") for r in resident]
    hit = _models.match_served(matched, ids) if matched else ""
    if hit:
        entry = next((r for r in resident if str(r.get("id")) == hit), None)
        # The resident id is the exact string the engine reports holding
        # (`llama3.2:latest` for a configured `llama3.2`) — the honest one.
        return "resident", hit, entry, True
    return "idle", matched, None, True


def _loaded_models() -> list[dict]:
    procs = _gpu_model_processes()
    rows = procs if procs else _docker_model_containers()

    # Cross-check each configured backend: the row serving it gets tagged (and
    # takes the config's display label), engines invisible to process telemetry
    # still get a row, and a configured-but-unreachable engine is surfaced as
    # offline. Identity comes from config or the live API — never from guessing
    # what a GPU process "probably" is.
    from . import models as _models
    brain = _models.effective_brain()
    brain_backend = str(brain.get("backend_id") or "")

    for b in _configured_backends():
        # One parser for "what is this engine serving", shared with the setup
        # wizard. This hardcoded `/models` and the OpenAI `data[].id` shape, so
        # an Ollama backend answered through its /v1 compatibility layer by luck
        # and a llama.cpp one was asked at whichever path happened to be right.
        # The key goes with it: without it a token-protected local engine
        # answers 401 and a perfectly healthy brain was reported offline.
        # A backend that is not on this box is never probed. /api/hardware is
        # polled every 2s by every open client, so probing a cloud provider here
        # would send the owner's API key off the box on a timer to learn
        # something the row does not use: what is in THIS machine's memory.
        local = bool(b.get("local", True))
        key = str(b.get("api_key") or "")
        reachable, served, resident = False, [], None
        if local:
            reachable, served = _models.probe_serving(
                str(b.get("url", "")), str(b.get("engine", "")), key, timeout=1.5)
            if reachable:
                resident = _models.probe_resident(
                    str(b.get("url", "")), str(b.get("engine", "")), key, timeout=1.5)
        state, confirmed, entry, measured = _backend_state(b, reachable, served, resident)

        label = str(b.get("label") or "").strip()
        row = _match_backend_row(rows, served or [str(b.get("model") or "")])
        if row is None and served:
            # Engine is up but no process exposed a model id (bare workers on a
            # platform without /proc, container boundaries, …): claim the one
            # unnamed row of the same engine kind rather than duplicating it.
            engine = str(b.get("engine") or "").lower()
            row = next((x for x in rows
                        if not x.get("model_id") and not x.get("backend")
                        and engine and engine in str(x.get("name", "")).lower()), None)
            if row is not None:
                row["model_id"] = served[0]
                row["model"] = _short_model_name(served[0], runtime=row.get("name"))

        if row is not None:
            row["backend"] = b.get("id")
            row["local"] = local
            # `implicit` = "served because nothing was configured, not because
            # anyone chose it". load_backends() stamps it precisely so a UI can
            # disclaim the row; dropping it here is why the monitor presented a
            # never-configured default as a peer of live models.
            row["implicit"] = bool(b.get("implicit"))
            # A GPU compute process holding memory IS the weights — the
            # strongest residency evidence there is, so it outranks what the
            # engine says about itself. A docker row is not: it means the
            # container is up, which for an Ollama container says nothing about
            # whether a model is loaded. Letting container liveness assert "In
            # memory" over an observed-empty /api/ps is exactly the disk-listing
            # lie this change exists to end.
            gpu_holds_it = (row.get("source") == "nvidia-smi"
                            and row.get("status") == "loaded")
            row["state"] = "resident" if gpu_holds_it else state
            # Whether that "In memory" was READ or derived. A GPU process holding
            # the weights is the strongest possible reading; the `serves_resident`
            # path is a conclusion about the engine's nature, not an observation.
            row["state_measured"] = True if gpu_holds_it else measured
            # The config's display label belongs to the config's model. When the
            # engine turns out to be serving something else — an ava.yaml that
            # drifted from what was actually launched — showing the label would
            # print a model name that is not in memory anywhere on this box. The
            # observed identity wins; being tied to this backend is still true.
            #
            # `label != id` because `load_backends` defaults an absent label to the
            # backend's ID, and that default is not a name anybody chose. Honouring
            # it made the picker read "local" for a row whose model is
            # "dolphin3:8b" — the config's filing name in place of the thing it
            # names. A label the operator actually wrote still wins.
            if (label and label != str(b.get("id") or "")
                    and (confirmed or not str(b.get("model") or "").strip())):
                row["model"] = label
            continue

        # One row per backend — never one per model the engine happens to have
        # on disk. Ollama lists every pulled tag, so fanning those out invented
        # a brain per tag, each claiming to be in memory.
        mid = confirmed or str(b.get("model") or "")
        size_mb = None
        if entry and entry.get("size_bytes"):
            size_mb = round(entry["size_bytes"] / (1024 * 1024), 1)
        rows.append({
            "id": f"{b.get('id')}:{mid or state}",
            "name": _short_runtime_name(str(b.get("engine") or "")),
            "model": label or _short_model_name(mid, runtime=str(b.get("engine") or "")),
            "model_id": mid or None,
            "memory_mb": size_mb,
            "memory_gb": round(size_mb / 1024, 2) if size_mb is not None else None,
            "gpu_util": None, "pid": None,
            "status": "loaded" if state == "resident" else (
                "offline" if state == "offline" else "empty"),
            "state": state,
            "state_measured": measured,
            "source": "api", "cmd": "",
            "backend": b.get("id"),
            "local": bool(b.get("local", True)),
            "implicit": bool(b.get("implicit")),
            # Ollama reports how much of the model sits in VRAM vs system RAM;
            # on a CPU-only box that split is the answer to "is my GPU doing
            # anything for this model", which no other reading gives us.
            "vram_mb": (round(entry["vram_bytes"] / (1024 * 1024), 1)
                        if entry and entry.get("vram_bytes") else None),
            "served": served,
        })

    # The brain is named by config, so it is knowable with zero telemetry —
    # which is exactly the case that used to render an empty panel. The agent
    # runtime answers turns inside its own sandbox, so it has no backend row of
    # its own and gets one here; otherwise the brain is whichever backend row
    # the resolver picked.
    if brain.get("source") == "agent":
        # Emitted even before the sandbox's model name is known: the row's job
        # is to say what answers turns, and "the agent runtime, name pending" is
        # a truer answer than a router backend that will not see the turn.
        rows.append({
            "id": "agent:sandbox",
            "name": "Agent sandbox",
            "model": str(brain.get("label") or brain.get("model_id")
                         or "Agent sandbox"),
            "model_id": brain.get("model_id") or None,
            "memory_mb": None, "memory_gb": None, "gpu_util": None, "pid": None,
            "status": "empty",
            # The sandbox holds the endpoint; we cannot see inside it, so
            # residency is unknown rather than absent.
            "state": "unknown" if brain.get("local") else "remote",
            "source": "agent", "cmd": "",
            "backend": "", "local": bool(brain.get("local")),
            "vram_mb": None, "served": [],
        })
        rows[-1]["role_key"] = "brain"
    elif brain_backend:
        for r in rows:
            if r.get("backend") == brain_backend:
                r["role_key"] = "brain"
                break

    # A row for a backend that is not on this box only earns its place in a
    # panel about this box's memory when it IS the brain — the user must be
    # able to see what Ava thinks with, wherever it runs.
    rows = [r for r in rows
            if r.get("local", True) or r.get("role_key") == "brain"]

    for r in rows:
        r.setdefault("state", "resident" if r.get("status") == "loaded" else "unknown")
        r.setdefault("local", True)
        r.setdefault("implicit", False)
        if not r.get("role_key"):
            r["role_key"] = _role_key(r.get("model_id") or r.get("model"))

    # The brain first — it is what the panel is for. Everything else by weight.
    rows.sort(key=lambda x: (x.get("role_key") != "brain",
                             -(x.get("memory_mb") or 0)))

    # Both passes below need the FINISHED row. `_dedupe` lifts `role_key`,
    # `backend` and `implicit` onto whichever row survives a merge, and the
    # component evidence `_attach_components` adds is the only thing that can
    # name a process whose command line named nothing. Deriving either before
    # this point reads a row that is still half-built.
    rows = _name_from_evidence(_attach_components(_dedupe(rows)))
    for r in rows:
        r["app"] = _owning_app(r)
        r["relation"] = _relation(r)
    return rows


def _model_key(row: dict) -> str:
    """The identity two rows must share to BE the same model. "" = unidentifiable."""
    mid = str(row.get("model_id") or "").strip().lower()
    if not mid or _is_blob_name(mid):
        # No identity: an unnamed worker, or a digest. Two of those are not
        # evidence of one model — several genuinely separate processes look
        # exactly like this — so they are never merged.
        return ""
    # `some-org/Some-Model-7B` and `Some-Model-7B` are one model named by two
    # layers: a launcher's `--model` carries the org prefix, the engine's own API
    # often does not.
    return mid.rsplit("/", 1)[-1]


def _dedupe(rows: list[dict]) -> list[dict]:
    """One model, one row. The panel's flat invariant.

    Rows arrive from two independent observers — GPU processes and the engines'
    own APIs — and they are SUPPOSED to converge: `_match_backend_row` folds a
    backend into the process row serving it. When that match misses, both survive,
    and the picker lists the same model twice with two different memory figures and
    no way to tell which is real.

    This is the net under that. Deliberately keyed on model identity only, so it
    can never collapse two genuinely distinct models, and it keeps the richer row:
    a row tied to a backend knows what it is, and one from `nvidia-smi` has a PID
    and a measured size. Merging keeps both halves rather than picking a winner.
    """
    out: list[dict] = []
    seen: dict[str, dict] = {}
    for r in rows:
        key = _model_key(r)
        if not key:
            out.append(r)
            continue
        first = seen.get(key)
        if first is None:
            seen[key] = r
            out.append(r)
            continue
        # Same model, twice. Keep the row already in place and lift anything the
        # duplicate knew that it did not — a PID, a measurement, a backend tie.
        for field in ("pid", "memory_mb", "memory_gb", "vram_mb", "gpu_util",
                      "backend", "cmd", "role_key", "implicit"):
            if not first.get(field) and r.get(field):
                first[field] = r[field]
        # An observed GPU process outranks an API's word on residency: the memory
        # is the weights. Only ever upgrades — never talks a row DOWN to idle.
        if r.get("source") == "nvidia-smi" and r.get("status") == "loaded":
            first["state"] = "resident"
            first["status"] = "loaded"
        if r.get("served") and not first.get("served"):
            first["served"] = r["served"]
    return out


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


# Filesystem types that are a window onto storage OUTSIDE this machine: a
# Windows path bind-mounted into WSL2 or Docker Desktop (drvfs, 9p, virtiofs), or
# a network share. statvfs on these reports the real volume, so the numbers they
# give need no caveat — which is the whole reason the caveat below is keyed on
# the filesystem type rather than on "am I in a container".
_HOST_BACKED_FS = frozenset({"9p", "virtiofs", "drvfs", "cifs", "smb3",
                             "nfs", "nfs4"})


def _fstype_of(path: str) -> str:
    """Filesystem type of the mount covering `path`, "" if not knowable.

    Longest matching mount point wins: `/` and `/data` are both prefixes of
    `/data/models`, and only the deeper one describes what it is stored on.
    """
    try:
        target = os.path.realpath(path)
        best, best_type = "", ""
        with open("/proc/self/mountinfo") as f:
            for line in f:
                # "<fields…> - <fstype> <source> <opts>". The separator is the
                # only reliable split: the optional-fields count before it varies.
                head, _, tail = line.partition(" - ")
                fields, rest = head.split(), tail.split()
                if len(fields) < 5 or not rest:
                    continue
                # Mount points with spaces arrive octal-escaped; \040 is the only
                # one that occurs in practice and decoding it costs nothing.
                point = fields[4].replace("\\040", " ")
                if (target == point or target.startswith(point.rstrip("/") + "/")) \
                        and len(point) >= len(best):
                    best, best_type = point, rest[0]
        return best_type
    except (OSError, ValueError):
        return ""


def _disk_cap(fstype: str) -> tuple[bool, str | None]:
    """(capped, why) — is this filesystem a slice of a machine we cannot see?

    Deliberately the same shape and the same vocabulary as hwinfo._pool_cap,
    because it is the same lie told about a different resource: a number that is
    correct for everything running inside, and is not the machine's.

    WSL2 is the case that reaches users. Its ext4 lives on a SPARSE .vhdx whose
    nominal maximum defaults to ~1 TB, so `df` reports that ceiling and its own
    emptiness no matter how full the Windows volume behind it is. Measured on a
    Windows laptop: Ava said 928.5 GB free while C: had 156 GB.

    A Windows path mounted back in (drvfs and friends) is exempt — statvfs there
    reads the real volume. And as with the memory pool, the host's true figure is
    NOT readable from in here, so flagging the reading without inventing a number
    is the honest half we can deliver.
    """
    if fstype in _HOST_BACKED_FS:
        return False, None
    if "microsoft" in (hwinfo._read_sysfs("/proc/sys/kernel/osrelease") or "").lower():
        return True, "wsl2-vm"
    return False, None


def _disk(path: str | None = None) -> dict:
    """Capacity of the filesystem Ava actually writes to: total / used / free.

    This measured `/` — the CONTAINER's root filesystem — which on Docker
    Desktop is an overlay on the sparse vhdx described in `_disk_cap`. It
    published the virtual disk's nominal capacity and its own emptiness as
    though they were the machine's, overstating free space by 772 GB on the
    laptop that surfaced it. The one job this number has is answering "can I
    pull this model", so an overstatement greenlights a pull that cannot finish.

    AVA_HOME is both the honest mount and the useful one: models, weights and
    every byte of state land under it, so it is the filesystem a pull fills.
    """
    if path is None:
        try:
            from . import settings
            path = str(settings.AVA_HOME)
        except Exception:  # noqa: BLE001 — unimportable settings must not blank the panel
            path = "/"
    try:
        u = shutil.disk_usage(path)
    except Exception:  # noqa: BLE001
        return {"total_gb": None, "used_gb": None, "free_gb": None,
                "used_pct": None, "path": path, "capped": False, "cap_kind": None}
    capped, cap_kind = _disk_cap(_fstype_of(path))
    g = 1024 ** 3
    return {"total_gb": round(u.total / g, 1), "used_gb": round(u.used / g, 1),
            "free_gb": round(u.free / g, 1),
            "used_pct": round(100 * u.used / u.total) if u.total else None,
            # Which volume this describes, and whether it is the machine's.
            "path": path, "capped": capped, "cap_kind": cap_kind}


def _role_key(model) -> str:
    """What a row is FOR, as a stable machine token the UI renders copy for.

    Deliberately NOT a sentence: the backend returns facts and the frontend owns
    owner-facing wording (CLAUDE.md). Emitting "Ava's brain (chat inference)"
    here is why the monitor and Setup → Agent described the same model in two
    unrelated vocabularies.

    "brain" is NOT decided here. It is not a property of a model's name — it is
    whichever backend the router will actually send a turn to, which only
    `models.effective_brain()` knows. Any truthy backend tag used to win, so a
    second configured backend, and even an unreachable one, both read as the
    brain.
    """
    return ""


def _relation(row: dict) -> str:
    """Whose this row is, as a closed machine token (see `_RELATIONS`).

    Reads `role_key` rather than asking "is this the brain?" a second time.
    That question has exactly one answer — models.effective_brain() — and a
    second derivation of it is the bug this module already exists to prevent
    (see the BrainVisibility tests, and alloc/__init__.py's refusal to
    re-derive it). Everything else here is a fact already on the row.

    Order matters: a backend tie outranks app ownership, because an engine the
    owner pointed Ava at IS Ava's, even when the app that ships it also claims
    it. Only a row nothing claims is foreign.
    """
    if row.get("role_key") == "brain":
        return "brain"
    if row.get("backend"):
        return "configured"
    if row.get("app"):
        return "app"
    return "foreign"


def _model_role(model, backend_id: str | None = None) -> str:
    """Connector-declared role text for a row, or "".

    Kept because connector roles are registry-declared copy that must stay
    self-contained for agent tools (the documented exception to "copy lives in
    the frontend"). Ava's own roles now travel as `role_key`.
    """
    if backend_id:
        return "Ava's brain (chat inference)"
    m = (model or "").lower()
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
    named task instead of a bare percentage.

    Jobs come from connectors that declare a `jobs:` block (see
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
    return jobs


def stats() -> dict:
    """One live snapshot of the device's hardware."""
    models = _loaded_models()
    for m in models:
        # Only the row the router will actually send a turn to is the brain —
        # `backend` merely means "we tied this row to some configured backend".
        m["role"] = _model_role(m.get("model_id") or m.get("model"),
                                "brain" if m.get("role_key") == "brain" else None)
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
