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

DEFAULT_MODELS = {
    "chat": {"engine": "vllm",
             "id": "Qwen/Qwen2.5-7B-Instruct", "tier": "medium"},
    "fast": {"engine": "ollama", "id": "llama3.1:8b", "tier": "small"},
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

    A partial override (e.g. declaring only `fast`) must NOT delete the default
    chat role — that silently killed `pull --auto`. Set a role to null/false in
    ava.yaml to genuinely remove it.
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
            "gguf": os.path.join(base, "gguf")}


def ensure_dirs() -> dict:
    d = dirs()
    for k in ("hf", "ollama", "gguf"):
        os.makedirs(d[k], exist_ok=True)
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


def gguf_present(spec: dict, gguf_dir: str) -> bool:
    return os.path.isfile(os.path.join(gguf_dir, spec.get("id", "")))


def present(spec: dict, model_dirs: dict) -> bool:
    eng = spec.get("engine")
    if eng == "vllm":
        return hf_present(spec["id"], model_dirs["hf"])
    if eng == "ollama":
        return ollama_present(spec["id"], model_dirs["ollama"])
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

    vLLM needs a CUDA GPU here, so it can't serve on Apple Silicon or a CPU-only
    box — pulling tens of GB of weights there is a trap. Every other local engine
    (Ollama, llama.cpp, MLX, LM Studio) and all cloud engines run anywhere. When
    the platform is unknown we don't block (return True) — degrade, never brick.

    **ROCm is opt-in, not implied.** This docstring used to say "CUDA/ROCm" while
    the code allowed only the two NVIDIA classes — so the doc promised a
    capability the code refused. Rather than make the doc true by green-lighting
    AMD, the gate stays NVIDIA-only by default: vLLM-on-ROCm is real but
    build- and gfx-target-specific, and silently steering an AMD owner into a
    tens-of-GB pull that their build cannot serve is the exact trap this function
    exists to prevent. An owner who knows their stack works sets
    `inference.allow_vllm_rocm` and gets it.
    """
    eng = (engine or "").strip().lower()
    if eng == "vllm":
        plat = platform_label()
        if plat in ("linux-nvidia", "windows-nvidia"):
            return True
        if plat == "linux-amd":
            try:
                from . import settings
                return bool(settings.get_bool("inference.allow_vllm_rocm", False))
            except Exception:  # noqa: BLE001 — no settings (script use): stay safe
                return False
        return False
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


def models_url(base_url: str, engine: str = "") -> str:
    """Where to ask a running engine what it is serving.

    Ollama's OpenAI-compatible base ends in /v1 but its native list is at
    /api/tags off the root, so the /v1 has to come off — the same shape as
    setup_wizard._health_url, which is why both derive the path from the engine
    registry rather than hardcoding it.
    """
    from .engines import get as _engine_get

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    spec = _engine_get(engine) if engine else None
    path = getattr(spec, "models_path", "/models") if spec else "/models"
    if path.startswith("/api/") and base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base + path


def probe_serving(base_url: str, engine: str = "", key: str = "",
                  timeout: float = 2.0) -> tuple[bool, list[str]]:
    """(reachable, ids). Reachable is NOT "returned something".

    An engine that is up with an empty store is reachable and serving nothing;
    an engine that is down is neither. Collapsing those two into "no ids" would
    report a running-but-empty Ollama as offline, and the dashboard would send
    the owner hunting for a service that is fine.
    """
    ids = _fetch_served(base_url, engine, key, timeout)
    return (ids is not None), (ids or [])


def served_models(base_url: str, engine: str = "", key: str = "",
                  timeout: float = 2.0) -> list[str]:
    """What this engine is ACTUALLY holding, as the exact ids it will accept.

    Ava sends the model id verbatim, so "close enough" is not a category: vLLM is
    case-sensitive about `Qwen/Qwen2.5-7B-Instruct`, and Ollama reports a pulled
    `llama3.2` as `llama3.2:latest`. Setup asked the user to type that string from
    memory; this is how it stops having to.

    Two shapes, because there are two: OpenAI-compatible returns
    `{"data": [{"id": ...}]}` and Ollama's native /api/tags returns
    `{"models": [{"name": ...}]}`. Returns [] on any failure — an engine that is
    down or slow must degrade to "we do not know", never to an exception on a
    setup screen.
    """
    return _fetch_served(base_url, engine, key, timeout) or []


def _fetch_served(base_url: str, engine: str, key: str,
                  timeout: float) -> list[str] | None:
    """The ids, or None when the engine could not be reached at all."""
    url = models_url(base_url, engine)
    if not url:
        return None
    try:
        import requests
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = requests.get(url, timeout=timeout, headers=headers)
        if not r.ok:
            return None
        body = r.json()
    except Exception:  # noqa: BLE001 — unreachable, slow, or not JSON
        return None
    out: list[str] = []
    if isinstance(body, dict):
        for item in (body.get("data") or []):
            if isinstance(item, dict) and str(item.get("id") or "").strip():
                out.append(str(item["id"]).strip())
        for item in (body.get("models") or []):
            if isinstance(item, dict) and str(item.get("name") or "").strip():
                out.append(str(item["name"]).strip())
    # Stable order, no duplicates — this feeds a <select> and a comparison.
    return sorted(dict.fromkeys(out))


def resident_url(base_url: str, engine: str = "") -> str:
    """Where to ask a running engine what it is holding IN MEMORY, or "".

    Same /v1-stripping shape as models_url, and for the same reason: Ollama's
    OpenAI-compatible base ends in /v1 but its native endpoints hang off the
    root. "" means this engine declares no residency endpoint — the caller must
    then say "unknown", never "not loaded".
    """
    from .engines import get as _engine_get

    base = (base_url or "").strip().rstrip("/")
    if not base:
        return ""
    spec = _engine_get(engine) if engine else None
    path = getattr(spec, "resident_path", None) if spec else None
    if not path:
        return ""
    if path.startswith("/api/") and base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    return base + path


def probe_resident(base_url: str, engine: str = "", key: str = "",
                   timeout: float = 2.0) -> list[dict] | None:
    """What this engine currently holds in memory — the honest residency read.

    Three-valued on purpose, mirroring `probe_serving`'s reachable/ids split:

      None  we cannot know (engine declares no residency endpoint, or the call
            failed). The caller must report unknown residency.
      []    the engine answered and is holding nothing. This is the ordinary
            state of an idle Ollama: models pulled, none resident.
      [...] `{"id", "size_bytes", "vram_bytes"}` per resident model.

    Collapsing None into [] is the bug this exists to prevent: it would report
    every engine that cannot be asked as empty.
    """
    url = resident_url(base_url, engine)
    if not url:
        return None
    try:
        import requests
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        r = requests.get(url, timeout=timeout, headers=headers)
        if not r.ok:
            return None
        body = r.json()
    except Exception:  # noqa: BLE001 — unreachable, slow, or not JSON
        return None
    if not isinstance(body, dict):
        # A shape we do not understand is "we could not find out", not "it is
        # holding nothing" — returning [] here would report an engine we failed
        # to read as idle, which is the same lie in a different coat.
        return None
    # Ollama /api/ps: {"models":[{"name","size","size_vram",...}]}. Kept
    # tolerant of the OpenAI `data[].id` envelope so an engine that adopts a
    # residency endpoint in that shape needs no code here.
    items = body.get("models")
    if items is None:
        items = body.get("data")
    if items is None or not isinstance(items, list):
        return None          # answered, but not with a model list we can read
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        mid = str(item.get("name") or item.get("model")
                  or item.get("id") or "").strip()
        if not mid:
            continue
        out.append({"id": mid,
                    "size_bytes": _as_int(item.get("size")),
                    "vram_bytes": _as_int(item.get("size_vram"))})
    return out


def _as_int(v) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def effective_brain() -> dict:
    """The one model that answers a chat turn right now.

    THE single answer to "what is Ava thinking with", so that the hardware
    monitor, Setup → Agent, the chat header and /api/health stop each deriving
    it from a different corner of the config and disagreeing. Every field is
    read from configuration — this says nothing about whether the thing is up,
    which is a separate, observed question (see hardware._loaded_models).

    Resolution order is the order a turn actually takes:
      1. the agent runtime's sandbox model, when that runtime is live — it
         bypasses the router entirely, so whatever the router would have
         chosen is not what answers;
      2. the configured brain: `inference.roles.chat`, else `inference.primary`;
      3. whatever the router would serve first — a configured backend, or the
         env/built-in fallback, which is then flagged `implicit`.

    `source` names which branch won, so a UI can say "agent sandbox" or
    "built-in default" without re-deriving any of it.
    """
    out = {"source": "none", "backend_id": "", "model_id": "", "label": "",
           "engine": "", "base_url": "", "api_key": "", "local": False,
           "implicit": False}

    try:  # the agent runtime answers turns itself when it is available
        # `runtime.active()`, because that IS the "when it is available" test:
        # it hands back the NemoClaw singleton only once available() has
        # confirmed the CLI and the sandbox, and the Direct floor otherwise.
        # This read `from .runtime import nemoclaw`, which binds the package's
        # FACTORY FUNCTION, not the runtime — so `.status()` raised
        # AttributeError straight into the except below and branch 1 never
        # fired once. Every caller silently fell through to the config.
        #
        # sandbox_info(wait=False), not status(): this resolver is reached from
        # the public /api/health, which must never block on a `nemoclaw list`
        # shell-out (see NemoClawRuntime.sandbox_info) — and status() would add
        # a doctor probe whose answer nothing here reads.
        # WHICH RUNTIME answers decides this branch; the model id only fills in
        # its name. Gating the branch on the id instead made the answer depend
        # on whether that 120s cache happened to be warm: the first caller after
        # a restart got the router's backend and the next one got the sandbox,
        # so /api/health and the hardware monitor named different models on one
        # screen — the exact disagreement this resolver exists to end. `active()`
        # is config plus availability, with no cache to be cold.
        from . import runtime
        rt = runtime.active()
        if rt.name != "direct":
            info = (getattr(rt, "sandbox_info", None)
                    and rt.sandbox_info(wait=False)) or {}
            mid = str(info.get("model") or "").strip()
            return {**out, "source": "agent", "backend_id": "",
                    # An empty id is honest and self-correcting: the label says
                    # what is answering, and the name appears a moment later
                    # when the background refresh lands. Reporting a backend
                    # that is NOT serving the turn would not self-correct.
                    # Label stays EMPTY until the name is known, so the caller's
                    # own fallback copy shows ("Agent sandbox"), not a runtime
                    # id the owner never chose and would not recognise.
                    "model_id": mid, "label": mid.rsplit("/", 1)[-1],
                    "engine": str(info.get("provider") or "").strip() or rt.name,
                    # The sandbox process is on this box, but IT holds the model
                    # endpoint and we do not — so residency is unobservable
                    # rather than absent, which is what `local` buys the caller.
                    "local": rt.name != "remote"}
    except Exception:  # noqa: BLE001 — the runtime is optional
        pass

    try:
        from . import router_app
        backends = router_app.load_backends()
    except Exception:  # noqa: BLE001 — never let config break the resolver
        backends = []
    if not backends:
        return out

    want = ""
    try:
        inf = settings.get("inference") or {}
        if isinstance(inf, dict):
            roles = inf.get("roles") or {}
            want = str((roles.get("chat") if isinstance(roles, dict) else "")
                       or inf.get("primary") or "").strip()
    except Exception:  # noqa: BLE001
        want = ""

    b = next((x for x in backends if x.get("id") == want), backends[0])
    implicit = bool(b.get("implicit"))
    from .model_fit import is_local
    return {**out,
            "source": "implicit" if implicit else "configured",
            "backend_id": str(b.get("id") or ""),
            "model_id": str(b.get("model") or ""),
            "label": str(b.get("label") or b.get("model") or ""),
            "engine": str(b.get("engine") or ""),
            "base_url": str(b.get("url") or ""),
            "api_key": str(b.get("api_key") or ""),
            "local": is_local(b),
            "implicit": implicit}


def match_served(model: str, served: list[str]) -> str:
    """The id the engine will accept for `model`, or "" if it holds no such thing.

    Forgiving in exactly one direction: a bare Ollama tag matches its `:latest`
    form, because `ollama pull llama3.2` stores `llama3.2:latest` and the user
    typed — or the installer wrote — the bare name. Everything else is exact,
    since guessing at a near-miss is how you get a config that looks right and
    fails on the first message.
    """
    want = (model or "").strip()
    if not want or not served:
        return ""
    if want in served:
        return want
    if ":" not in want:
        for s in served:
            if s.rsplit(":", 1)[0] == want:
                return s
    return ""


__all__ = [
    "DEFAULT_MODELS", "OLLAMA_CHAT", "LOCAL_CHAT_ENGINES",
    "manifest", "dirs", "ensure_dirs", "present", "hf_present", "ollama_present",
    "gguf_present", "platform_label", "detected_tier",
    "engine_servable_here", "resolve_auto", "roles_status",
    "models_url", "served_models", "match_served", "probe_serving",
    "resident_url", "probe_resident", "effective_brain",
]
