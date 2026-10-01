"""Read-only NVIDIA process inventory for node_exporter's textfile collector.

Run this module on the monitored host, not on the Ava bridge host. Only model
identities, runtime names, PIDs and allocations leave the process: never full
command lines, environment variables, prompts or absolute model paths. Python's
standard library is sufficient; no Ava settings or inference runtime is loaded.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import math
import os
from datetime import datetime
from pathlib import Path, PurePosixPath
import re
import subprocess
import time


_OPAQUE = re.compile(r"^(?:(?:sha\d{3}[-:])?[0-9a-f-]{16,}|\d+|model:[\w-]+)$", re.I)
_WEIGHTS = {".safetensors", ".gguf", ".ckpt", ".pth", ".pt", ".onnx", ".bin"}
_GENERIC = {"python", "python3", "model", "unknown", "unnamed", "models", "weights",
            "snapshots", "blobs", "pytorch_model", "diffusion_pytorch_model"}
_SHARD = re.compile(r"^(?:model|pytorch_model|diffusion_pytorch_model)[-_]\d+(?:-of-\d+)?$", re.I)


def model_identity(value: str) -> str:
    """A portable model id, never a cache revision or an absolute host path."""
    value = value.strip().replace("\\", "/").rstrip("/")
    if not value or len(value) > 512 or re.search(r"[\x00-\x1f\x7f]", value):
        return ""
    parts = PurePosixPath(value).parts
    for part in parts:
        if part.startswith("models--"):
            return model_identity(part.removeprefix("models--").replace("--", "/"))
    tail = parts[-1] if parts else ""
    stem = PurePosixPath(tail).stem if PurePosixPath(tail).suffix.lower() in _WEIGHTS else tail
    absolute = value.startswith("/") or re.match(r"^[A-Za-z]:/", value)
    repository = not absolute and "/" in value and PurePosixPath(tail).suffix.lower() not in _WEIGHTS
    if not repository and (_OPAQUE.fullmatch(stem) or stem.lower() in _GENERIC or _SHARD.fullmatch(stem)):
        return ""
    if absolute:
        return tail
    return value


def _small_json(path: Path, limit: int = 16384) -> dict:
    """Read identity metadata, never a weight file or an unbounded document."""
    try:
        if not path.is_file():
            return {}
        with path.open("rb") as stream:
            raw = stream.read(limit + 1)
        data = json.loads(raw) if len(raw) <= limit else None
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _process_path(proc: Path, pid: int, value: str) -> Path:
    # A container path belongs to that process's mount namespace, not to the
    # bridge or collector host. Relative --model paths belong to its cwd.
    path = PurePosixPath(value.replace("\\", "/"))
    base = proc / str(pid) / ("root" if path.is_absolute() else "cwd")
    return base / str(path).lstrip("/")


def _artifact_name(path: Path) -> tuple[str, str]:
    """Optional portable label beside an exact weight file or model directory."""
    sidecar = (path.with_name(path.name + ".ava-model.json")
               if path.suffix.lower() in _WEIGHTS else path / "ava-model.json")
    data = _small_json(sidecar)
    if not data and path.suffix.lower() in _WEIGHTS:
        data = _small_json(path.parent / "ava-model.json")
    name, mid = data.get("name"), data.get("id")
    # Sidecar identities must be portable, not absolute paths or process names.
    if not isinstance(name, str) or model_identity(name) != name.strip():
        return "", ""
    if mid is None:
        mid = name
    if not isinstance(mid, str) or model_identity(mid) != mid.strip():
        return "", ""
    if not name.strip() or not mid.strip():
        return "", ""
    return mid.strip(), name.strip()


def _ollama_name(path: Path) -> tuple[str, str]:
    """Name an observed blob using its own store, with no daemon or registry.

    Ollama manifests use host/namespace/model/tag and a model layer digest.
    Multiple tags can share weights but differ in prompts/adapters: do not pick
    one arbitrarily. The configured engine API can still disambiguate that case.
    """
    if path.parent.name != "blobs" or not re.fullmatch(r"sha256[-:][0-9a-f]{64}", path.name):
        return "", ""
    digest = path.name.replace("sha256-", "sha256:", 1)
    root = path.parent.parent / "manifests"
    matches = set()
    try:
        for count, manifest in enumerate(root.glob("*/*/*/*")):
            if count >= 1024:
                return "", ""  # An incomplete search cannot establish uniqueness.
            layers = _small_json(manifest).get("layers", [])
            if not isinstance(layers, list) or not any(
                isinstance(layer, dict) and layer.get("digest") == digest
                and layer.get("mediaType") == "application/vnd.ollama.image.model"
                for layer in layers
            ):
                continue
            host, namespace, name, tag = manifest.relative_to(root).parts
            if host == "registry.ollama.ai":
                name = name if namespace == "library" else f"{namespace}/{name}"
            else:
                name = f"{host}/{namespace}/{name}"
            name = model_identity(f"{name}:{tag}")
            if name:
                matches.add(name)
    except OSError:
        return "", ""
    name = next(iter(matches)) if len(matches) == 1 else ""
    return name, name


def resolve_model(value: str, proc: Path, pid: int, *, component: bool = False) -> tuple[str, str]:
    """Ava's shared local/remote resolver, using observed model identities."""
    model = model_identity(value)
    if not value:
        return "", ""
    path = _process_path(proc, pid, value)
    declared = _artifact_name(path)
    if declared[0]:
        return declared
    ollama = _ollama_name(path)
    if ollama[0]:
        return ollama
    if model:
        if component and "models--" not in value and path.suffix.lower() != ".gguf":
            return model, ""  # A generic encoder file does not identify the whole model.
        display = PurePosixPath(model).name
        if PurePosixPath(display).suffix.lower() in _WEIGHTS:
            display = PurePosixPath(display).stem
        return model, display
    return "", ""


def component_identity(components: list[dict]) -> tuple[str, str]:
    # Unknown extra weights might belong to another model; all observed
    # components must agree before one model can name the entire process.
    identities = {(c.get("model_id", ""), c.get("model_name", "")) for c in components}
    if len(identities) == 1:
        model, name = next(iter(identities))
        if model and name:
            return model, name
    return "", ""


def _args(proc: Path, pid: int) -> list[str]:
    try:
        return (proc / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


def _model(args: list[str]) -> str:
    flags = {"--model", "--model-id", "--model-path", "--ckpt_name"}
    if args and "llama" in Path(args[0]).name.lower():
        flags.add("-m")
    for index, arg in enumerate(args):
        if arg in flags and index + 1 < len(args):
            return args[index + 1][:512]
        flag, sep, value = arg.partition("=")
        if sep and flag in flags:
            return value[:512]
    # vLLM also accepts `vllm serve MODEL` as a positional model identifier.
    for index, arg in enumerate(args[:-1]):
        if arg == "serve" and any("vllm" in a.lower() for a in args[:index]):
            value = args[index + 1]
            if value and not value.startswith("-"):
                return value[:512]
    return ""


def _owner(proc: Path, pid: int) -> tuple[int, list[str], str]:
    original = _args(proc, pid)
    current, seen = pid, set()
    for _ in range(8):
        if current <= 1 or current in seen:
            break
        seen.add(current)
        args = _args(proc, current)
        model = _model(args)
        if model:
            return current, args, model
        try:
            raw = (proc / str(current) / "stat").read_text()
            current = int(raw.rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
    return pid, original, ""


def _runtime(args: list[str], process_name: str) -> str:
    text = " ".join([*args, process_name]).lower()
    for needle, name in (("comfyui", "ComfyUI"), ("vllm", "vLLM"),
                         ("ollama", "Ollama"), ("llama", "llama.cpp"),
                         ("whisper", "Whisper")):
        if needle in text:
            return name
    return Path(process_name.strip()).name[:80] or "GPU process"


def _components(proc: Path, pid: int, *, include_paths: bool = False) -> list[dict]:
    paths: set[str] = set()
    try:
        for line in (proc / str(pid) / "maps").read_text(errors="replace").splitlines():
            parts = line.split(maxsplit=5)
            if len(parts) == 6:
                paths.add(parts[5].removesuffix(" (deleted)"))
    except OSError:
        pass
    try:
        for fd in (proc / str(pid) / "fd").iterdir():
            try:
                paths.add(str(fd.readlink()))
            except OSError:
                pass
    except OSError:
        pass
    out = []
    for name in sorted(paths):
        path = Path(name)
        if path.suffix.lower() not in _WEIGHTS and path.parent.name != "blobs":
            continue
        model, label = resolve_model(name, proc, pid, component=True)
        if path.suffix.lower() == ".bin" and not label:
            continue
        kind = "model"
        for directory, category in (("diffusion_models", "unet"), ("unet", "unet"),
                                    ("text_encoders", "clip"), ("clip", "clip"),
                                    ("vae", "vae"), ("loras", "loras")):
            if directory in path.parts:
                kind = category
                break
        opaque = not label and _OPAQUE.fullmatch(path.stem)
        item = {"name": "" if opaque else (label or path.name)[:512], "kind": kind}
        if label:
            item.update(model_id=model, model_name=label)
            if any(c.get("model_id") == model and c.get("model_name") == label
                   and c["kind"] == kind for c in out):
                continue
        if include_paths:
            item["path"] = name  # Local connector ownership only; never exported.
        if item not in out:
            out.append(item)
    return out[:128]


def _run_model(manifest: Path) -> tuple[str, str]:
    """Identity from a running local Diffusers generation manifest.

    The caller must establish that this PID has the run's output open for
    appending. A configuration file or a completed run alone proves nothing
    about which model the process is currently using.
    """
    try:
        with manifest.open("rb") as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return "", ""
        run = json.loads(raw)
        if not isinstance(run, dict):
            return "", ""
        generator = run.get("generator")
        # This manifest contract's Diffusers generator runs in this process.
        # ComfyUI/API generators run elsewhere and cannot name its GPU memory.
        if not isinstance(generator, dict) or generator.get("name") != "diffusers":
            return "", ""
        started = datetime.fromisoformat(run["images_started_at"].replace("Z", "+00:00"))
        finished = run.get("images_finished_at")
        if finished and datetime.fromisoformat(finished.replace("Z", "+00:00")) >= started:
            return "", ""
        value = generator.get("model_id")
        if not isinstance(value, str) or not value.strip() or len(value) > 512:
            return "", ""
        model = model_identity(value)
        return model, model.rsplit("/", 1)[-1]
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return "", ""


def _run_identity(proc: Path, pid: int) -> tuple[str, str]:
    """Join an active output writer to its sibling run.json, without reading output.

    We never scan run directories or read generated records/prompts. The open
    append descriptor ties the manifest to this live PID and stops an unrelated
    reader, an old run, or a process that has moved on from claiming its model.
    """
    folder = proc / str(pid)
    matches: set[tuple[str, str]] = set()
    try:
        for fd in (folder / "fd").iterdir():
            try:
                target = fd.readlink()
                if target.name != "generated.jsonl" or not target.is_absolute():
                    continue
                info = (folder / "fdinfo" / fd.name).read_text()
                flags = next(int(line.split()[1], 8) for line in info.splitlines()
                             if line.startswith("flags:"))
                # Linux /proc flags, even when fixtures are read on Windows:
                # O_WRONLY/O_RDWR and O_APPEND. Read-only observers don't count.
                if flags & 0o3 not in (1, 2) or not flags & 0o2000:
                    continue
                # Use the process's filesystem view, including container mounts.
                manifest = folder / "root" / str(target.parent).lstrip("/") / "run.json"
                identity = _run_model(manifest)
                if identity[0]:
                    matches.add(identity)
            except (OSError, ValueError, IndexError, StopIteration):
                continue
    except OSError:
        pass
    return next(iter(matches)) if len(matches) == 1 else ("", "")


def collect(proc: Path = Path("/proc"), run=subprocess.run) -> list[dict]:
    result = run(["nvidia-smi", "--query-compute-apps=pid,process_name,used_memory",
                  "--format=csv,noheader,nounits"], capture_output=True, text=True,
                 timeout=5, check=True)
    groups: dict[int, dict] = {}
    for fields in csv.reader(io.StringIO(result.stdout)):
        if len(fields) != 3:
            raise ValueError("Unrecognized GPU process inventory")
        pid = int(fields[0].strip())
        owner, args, model = _owner(proc, pid)
        model, model_name = resolve_model(model, proc, owner)
        if not model:
            model, model_name = _run_identity(proc, pid)
        group = groups.setdefault(owner, {"pid": owner, "runtime": _runtime(args, fields[1]),
                                          "model_id": model, "model_name": model_name,
                                          "memory_bytes": None,
                                          "components": []})
        try:
            memory = float(fields[2].strip()) * 1024 ** 2
        except ValueError:
            memory = None
        if memory is not None and math.isfinite(memory) and memory >= 0:
            group["memory_bytes"] = (group["memory_bytes"] or 0) + memory
        for component in _components(proc, pid):
            if component not in group["components"]:
                group["components"].append(component)
    for group in groups.values():
        if not group["model_id"]:
            group["model_id"], group["model_name"] = component_identity(group["components"])
    return list(groups.values())


def render(rows: list[dict], timestamp: float, success: bool = True) -> str:
    def labels(**values) -> str:
        return "{" + ",".join(f"{k}={json.dumps(str(v), ensure_ascii=False)}"
                               for k, v in values.items()) + "}"
    lines = [f"ava_gpu_inventory_timestamp_seconds {timestamp}",
             f"ava_gpu_inventory_success {int(success)}"]
    for row in rows if success else []:
        pid = row["pid"]
        model = model_identity(row["model_id"])
        lines.append("ava_gpu_process_info" + labels(
            pid=pid, runtime=row["runtime"], model_id=model,
            model_name=row.get("model_name", "")) + " 1")
        if row["memory_bytes"] is not None:
            lines.append("ava_gpu_process_memory_bytes" + labels(pid=pid)
                         + f" {row['memory_bytes']}")
        for component in row["components"]:
            if not component["name"]:
                continue  # Unnamed weights prevent attribution, but expose no raw hash.
            lines.append("ava_gpu_model_component_info" + labels(
                pid=pid, name=component["name"], kind=component["kind"]) + " 1")
    return "\n".join(lines) + "\n"


def write_snapshot(output: Path) -> None:
    try:
        content = render(collect(), time.time())
    except (OSError, ValueError, subprocess.SubprocessError):
        content = render([], time.time(), success=False)
    temporary = output.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(output)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    if args.interval < 1:
        parser.error("interval must be at least one second")
    while True:
        write_snapshot(args.output)
        if args.once:
            return
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
