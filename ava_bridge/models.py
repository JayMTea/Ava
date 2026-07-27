"""Model store — where weights live, what a fresh install pulls, and what fits.

Extracted from ava_cli.py so the bridge and the CLI share one implementation
instead of the bridge reaching backwards into the script. hub_api did this:

    sys.path.insert(0, settings.CODE_ROOT)
    import ava_cli
    return ava_cli                      # then call ava_cli._model_dirs(), etc.

which is a package importing a top-level script, through a mutated sys.path, to
reach four underscore-prefixed functions. It worked only because the CWD happened
to be the checkout, and it made `ava models` and Setup → Models two callers of one
private API that nothing stopped from drifting apart.

DEFAULT_MODELS is a STARTING POINT, not a requirement. Ava runs on any model its
engines can serve; these ids are only what a fresh `ava setup` seeds when the user
has not chosen. The user's `models:` block in ava.yaml overlays them (see
`manifest()`), and Setup → Models rewrites the inference backend outright.
Changing a default here changes what NEW installs pull — nothing else.
"""
from __future__ import annotations

import os
import shutil
import subprocess

from . import settings
from .model_fit import recommend_tier

gpusvc_SUBDIRS = ["checkpoints", "loras", "vae", "guidance net", "upscale_models",
                 "embeddings", "clip", "clip_vision", "unet", "weight_models",
                 "text_encoders"]

DEFAULT_MODELS = {
    "chat": {"engine": "vllm",
             "id": "Qwen/Qwen2.5-7B-Instruct", "tier": "medium"},
    "fast": {"engine": "ollama", "id": "llama3.1:8b", "tier": "small"},
    "image": {"engine": "gpu-service", "id": "gpu_model_base",
              "dest": "checkpoints",
              "url": "https://huggingface.co/example/gpu-model"
                     "resolve/main/gpu_model_base"},
}

# Chat models for boxes that can't serve the vLLM default (Apple Silicon,
# CPU-only): vLLM needs CUDA/ROCm, so `pull --auto` must never fetch it there.
# Keyed by the memory tier, mirroring the Apple-Silicon example in
# config.example.yaml (Ollama's OpenAI-compatible API on :11434 reads the same
# unified-memory pool).
OLLAMA_CHAT = {
    "large": {"engine": "ollama", "id": "llama3.1:70b", "tier": "large"},
    "medium": {"engine": "ollama", "id": "llama3.1:8b", "tier": "small"},
}

# Local chat engines Ava can seed a backend for on a non-CUDA box (all serve an
# OpenAI-compatible API; vLLM is deliberately absent — it needs a CUDA/ROCm GPU).
LOCAL_CHAT_ENGINES = {"ollama", "llamacpp", "gguf", "mlx", "mlx-lm",
                      "lmstudio", "lm-studio"}


# --- the manifest and where weights live ------------------------------------ #
def manifest() -> dict:
    """Default roles overlaid with the user's `models:` block.

    A partial override (e.g. declaring only `image`) must NOT delete the default
    chat/fast roles — that silently killed `pull --auto`. Set a role to
    null/false in ava.yaml to genuinely remove it.
    """
    m = settings.get("models", None)
    if not isinstance(m, dict) or not m:
        return dict(DEFAULT_MODELS)
    merged = {**DEFAULT_MODELS, **m}
    return {k: v for k, v in merged.items() if isinstance(v, dict)}


def dirs() -> dict:
    base = settings.models_dir()
    return {"root": base, "hf": os.path.join(base, "hf"),
            "ollama": os.path.join(base, "ollama"),
            "gpusvc": os.path.join(base, "gpusvc"),
            "gguf": os.path.join(base, "gguf")}


def ensure_dirs() -> dict:
    d = dirs()
    for k in ("hf", "ollama", "gpusvc", "gguf"):
        os.makedirs(d[k], exist_ok=True)
    for sub in gpusvc_SUBDIRS:
        os.makedirs(os.path.join(d["gpusvc"], sub), exist_ok=True)
    return d


# --- "is it already downloaded?" per engine --------------------------------- #
def hf_present(model_id: str, hf_dir: str) -> bool:
    safe = "models--" + model_id.replace("/", "--")
    return any(os.path.isdir(os.path.join(hf_dir, sub, safe)) for sub in ("hub", ""))


def ollama_env(ollama_dir: str) -> dict:
    return {**os.environ, "OLLAMA_MODELS": ollama_dir}


def ollama_present(tag: str, ollama_dir: str) -> bool:
    # Moved verbatim from ava_cli._ollama_present. Deliberately unchanged: this
    # is an extraction, and "while I'm here" behaviour edits during a move are
    # how a refactor turns into a regression nobody bisects to.
    if not shutil.which("ollama"):
        return False
    try:
        out = subprocess.run(["ollama", "list"], env=ollama_env(ollama_dir),
                             capture_output=True, text=True, timeout=10)
        return tag.split(":")[0] in out.stdout
    except Exception:  # noqa: BLE001
        return False


def gpusvc_present(spec: dict, gpusvc_dir: str) -> bool:
    dest = spec.get("dest") or "checkpoints"
    return os.path.isfile(os.path.join(gpusvc_dir, dest, spec.get("id", "")))


def gguf_present(spec: dict, gguf_dir: str) -> bool:
    return os.path.isfile(os.path.join(gguf_dir, spec.get("id", "")))


def present(spec: dict, model_dirs: dict) -> bool:
    eng = spec.get("engine")
    if eng == "vllm":
        return hf_present(spec["id"], model_dirs["hf"])
    if eng == "ollama":
        return ollama_present(spec["id"], model_dirs["ollama"])
    if eng == "gpu-service":
        return gpusvc_present(spec, model_dirs["gpusvc"])
    if eng in ("llamacpp", "gguf"):
        return gguf_present(spec, model_dirs["gguf"])
    return False


# --- what this box can actually run ----------------------------------------- #
def platform_label() -> str:
    """Coarse platform id for user-facing messages (never raises)."""
    try:
        from . import hwinfo
        return hwinfo.platform_id()
    except Exception:  # noqa: BLE001
        return "this platform"


def detected_tier() -> tuple[str, float | None]:
    try:
        from . import hwinfo
        avail = hwinfo.fit_memory().total_gb
    except Exception:  # noqa: BLE001
        avail = None
    if avail is None:
        return "cloud", None
    return recommend_tier(avail)[0], avail


def engine_servable_here(engine: str | None) -> bool:
    """Can THIS box actually run a local engine of this type?

    vLLM needs a CUDA/ROCm GPU, so it can't serve on Apple Silicon or a CPU-only
    box — pulling tens of GB of weights there is a trap. Every other local engine
    (Ollama, llama.cpp, MLX, LM Studio) and all cloud engines run anywhere. When
    the platform is unknown we don't block (return True) — degrade, never brick.
    """
    eng = (engine or "").strip().lower()
    if eng == "vllm":
        return platform_label() in ("linux-nvidia", "windows-nvidia")
    return True


def resolve_auto(tier: str, model_manifest: dict) -> tuple:
    """Pick (role, spec, note) for `ava models pull --auto`, platform-aware.

    Guarantees the returned spec's engine is servable on THIS box, so a high-RAM
    Mac (tier 'large') is never steered into the vLLM-only default it can't run.
    Substitutes a same-tier Ollama chat model, or downshifts to the servable
    'fast' role; `note` explains any substitution. Returns (None, None, note)
    when nothing local is servable (caller should point at a cloud provider).
    """
    role = {"large": "chat", "medium": "chat",
            "small": "fast", "tiny": "fast"}.get(tier)
    if not role or role not in model_manifest:
        return None, None, None
    spec = model_manifest[role]
    if engine_servable_here(spec.get("engine")):
        return role, spec, None
    plat = platform_label()
    blocked = (f"the default '{role}' model ({spec.get('engine')}: "
               f"{spec.get('id')}) can't be served on {plat}")
    sub = OLLAMA_CHAT.get(tier)
    if sub and engine_servable_here(sub.get("engine")):
        return role, sub, (f"{blocked} — substituting {sub['engine']}: "
                           f"{sub['id']} instead")
    fast = model_manifest.get("fast")
    if fast and engine_servable_here(fast.get("engine")):
        return "fast", fast, (f"{blocked} — downshifting to the "
                              f"'{fast.get('engine')}' model {fast.get('id')}")
    return None, None, f"{blocked}, and no local engine here can"


def roles_status() -> dict:
    """Everything Setup → Models needs in one call: each role, whether its
    weights are on disk, and the detected hardware tier."""
    man = manifest()
    d = dirs()
    tier, avail = detected_tier()
    return {
        "roles": [{"role": role, "id": spec.get("id"), "engine": spec.get("engine"),
                   "tier": spec.get("tier"), "present": present(spec, d)}
                  for role, spec in man.items()],
        "detected_tier": tier,
        "available_gb": avail,
        "dirs": d,
    }


__all__ = [
    "gpusvc_SUBDIRS", "DEFAULT_MODELS", "OLLAMA_CHAT", "LOCAL_CHAT_ENGINES",
    "manifest", "dirs", "ensure_dirs", "present", "hf_present", "ollama_present",
    "gpusvc_present", "gguf_present", "platform_label", "detected_tier",
    "engine_servable_here", "resolve_auto", "roles_status",
]
