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
from pathlib import Path
import subprocess
import time


def _args(proc: Path, pid: int) -> list[str]:
    try:
        return (proc / str(pid) / "cmdline").read_bytes().decode("utf-8", "replace").split("\0")
    except OSError:
        return []


def _model(args: list[str]) -> str:
    for index, arg in enumerate(args):
        if arg == "--model" and index + 1 < len(args):
            return args[index + 1][:512]
        if arg.startswith("--model="):
            return arg.split("=", 1)[1][:512]
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
    return Path(process_name).name[:80] or "GPU process"


def _components(proc: Path, pid: int) -> list[dict]:
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
        if path.suffix.lower() not in (".safetensors", ".gguf", ".ckpt", ".pth", ".pt"):
            continue
        kind = "model"
        for directory, category in (("diffusion_models", "unet"), ("unet", "unet"),
                                    ("text_encoders", "clip"), ("clip", "clip"),
                                    ("vae", "vae"), ("loras", "loras")):
            if directory in path.parts:
                kind = category
                break
        item = {"name": path.name[:512], "kind": kind}
        if item not in out:
            out.append(item)
    return out[:128]


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
        group = groups.setdefault(owner, {"pid": owner, "runtime": _runtime(args, fields[1]),
                                          "model_id": model, "memory_bytes": None,
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
    return list(groups.values())


def render(rows: list[dict], timestamp: float, success: bool = True) -> str:
    def labels(**values) -> str:
        return "{" + ",".join(f"{k}={json.dumps(str(v), ensure_ascii=False)}"
                               for k, v in values.items()) + "}"
    lines = [f"ava_gpu_inventory_timestamp_seconds {timestamp}",
             f"ava_gpu_inventory_success {int(success)}"]
    for row in rows if success else []:
        pid = row["pid"]
        # Absolute paths are not useful model identities on another machine.
        model = row["model_id"]
        if model.startswith("/"):
            model = Path(model).name
        lines.append("ava_gpu_process_info" + labels(
            pid=pid, runtime=row["runtime"], model_id=model) + " 1")
        if row["memory_bytes"] is not None:
            lines.append("ava_gpu_process_memory_bytes" + labels(pid=pid)
                         + f" {row['memory_bytes']}")
        for component in row["components"]:
            lines.append("ava_gpu_model_component_info" + labels(pid=pid, **component) + " 1")
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
