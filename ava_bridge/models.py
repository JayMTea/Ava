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
import re
import shutil
import subprocess

from . import settings
from .model_fit import recommend_tier

# NO DEFAULT CHAT MODEL. Ava names no brain.
#
# There was one — a `chat` role pinned to a specific 7B instruct checkpoint —
# and it was the last place Ava still asserted a model nobody had chosen.
# `router_app` had already dropped its synthesized backend for exactly that
# reason (see the note there, and CHANGELOG "Ava ships no default model"), which
# left the store manifest advertising a brain the router would never serve: the
# Model store listed it as `chat` on every fresh install, `pull --auto` fetched
# ~15 GB of it, and nothing was ever configured to run it.
#
# `fast` stays. It is a small Ollama tag offered as a fallback for constrained
# hardware, not a claim about what Ava thinks with, and it is what keeps
# `pull --auto` able to put something servable on a bare box. `resolve_auto`
# falls through to it now rather than giving up when `chat` is absent.
DEFAULT_MODELS = {
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

    A partial override must NOT delete the other declared roles — dropping them
    silently killed `pull --auto`. Set a role to null/false in ava.yaml to
    genuinely remove it.

    Ava declares no `chat` model of its own, so on a stock install this is the
    `fast` role and whatever the owner added. A `chat` role here is theirs.
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
    """Is this exact Ollama tag pulled?

    Compares the NAME column of `ollama list`, not a substring of the whole
    output. `tag.split(":")[0] in out.stdout` answered yes for
    `llama3.1:70b` when only `llama3.1:8b` was present — the repo name matches
    and the size does not — so a required model read as downloaded, the pull was
    skipped, and the first chat turn failed against a model the engine does not
    hold. It also matched a tag appearing anywhere else on the line, including
    inside another model's name.

    An untagged request means `:latest`, which is Ollama's own rule.
    """
    if not shutil.which("ollama"):
        return False
    want = tag if ":" in tag else f"{tag}:latest"
    try:
        out = subprocess.run(["ollama", "list"], env=ollama_env(ollama_dir),
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return False
    for line in out.stdout.splitlines()[1:]:      # skip the NAME/ID header
        name = line.split()[0] if line.split() else ""
        if name == want:
            return True
    return False


def gguf_path(spec: dict, gguf_dir: str) -> str:
    """Where a direct-URL GGUF lands on disk.

    ONE rule, because two existed and they disagreed. `gguf_present` joined
    `spec["id"]` while the downloader wrote `basename(id or url)`, so:

      * a URL-only spec (no `id`) checked `isfile(<dir>/)` — the directory —
        which is never a file, so the model read "missing" forever and every
        `ava models pull` re-downloaded gigabytes it already had; and
      * a slash-qualified id (`bartowski/model.gguf`) was written flat but
        looked for in a subdirectory, so a checksum re-check would hash a path
        nothing had written.

    `basename` is the downloader's rule and therefore the true one: it is what
    actually exists after a pull.
    """
    name = os.path.basename(spec.get("id") or spec.get("url") or "model.gguf")
    return os.path.join(gguf_dir, name)


def gguf_present(spec: dict, gguf_dir: str) -> bool:
    return os.path.isfile(gguf_path(spec, gguf_dir))


def present(spec: dict, model_dirs: dict) -> bool:
    eng = spec.get("engine")
    if eng == "vllm":
        return hf_present(spec["id"], model_dirs["hf"])
    if eng == "ollama":
        return ollama_present(spec["id"], model_dirs["ollama"])
    if eng in ("llamacpp", "gguf"):
        return gguf_present(spec, model_dirs["gguf"])
    return False


# --- how big is it, really? -------------------------------------------------- #
def _tree_gb(path: str) -> float | None:
    """Bytes on disk under `path`, in GB, or None if there is nothing there.

    `follow_symlinks=False` on the stat: a HuggingFace snapshot is a tree of
    symlinks into `blobs/`, so following them counts every weight file twice —
    once as the link target and once as the blob — and reports a 15 GB model as
    30 GB. The blobs are walked directly, so nothing is missed by not following.
    """
    total = 0
    seen: set[tuple[int, int]] = set()
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f), follow_symlinks=False)
            except OSError:
                continue
            # A blob hard-linked into two snapshots is one file on disk.
            key = (st.st_dev, st.st_ino)
            if st.st_nlink > 1:
                if key in seen:
                    continue
                seen.add(key)
            total += st.st_size
    return round(total / (1024 ** 3), 2) if total else None


def disk_size_gb(spec: dict, model_dirs: dict) -> float | None:
    """What this model actually occupies on disk, or None if it is not here.

    MEASURED. This is the honest input to the fit verdict — an estimate from the
    parameter count in a model's name is a guess about quantisation, and this is
    a number. Only available once the weights are downloaded, which is exactly
    when the owner most wants to know whether they were a mistake.
    """
    eng = spec.get("engine")
    try:
        if eng == "vllm":
            safe = "models--" + str(spec.get("id", "")).replace("/", "--")
            for sub in ("hub", ""):
                p = os.path.join(model_dirs["hf"], sub, safe)
                if os.path.isdir(p):
                    return _tree_gb(p)
            return None
        if eng in ("llamacpp", "gguf"):
            p = gguf_path(spec, model_dirs["gguf"])
            return (round(os.path.getsize(p) / (1024 ** 3), 2)
                    if os.path.isfile(p) else None)
        if eng == "ollama":
            return _ollama_size_gb(str(spec.get("id", "")), model_dirs["ollama"])
    except Exception:  # noqa: BLE001 — sizing is advisory; never break the list
        return None
    return None


_OLLAMA_SIZE = re.compile(r"\b([\d.]+)\s*(kB|KB|MB|GB|TB|KiB|MiB|GiB|TiB)\b")
_SIZE_GB = {"kb": 1 / 1e6, "mb": 1 / 1e3, "gb": 1.0, "tb": 1e3,
            "kib": 1 / (1024 ** 2), "mib": 1 / 1024, "gib": 1.0, "tib": 1024.0}


def _ollama_size_gb(tag: str, ollama_dir: str) -> float | None:
    """The SIZE column `ollama list` prints for this exact tag.

    Matched on the NAME column for the same reason `ollama_present` is: a
    substring test says yes for `llama3.1:70b` when only `llama3.1:8b` is
    pulled, and would then report the 8b's size as the 70b's.
    """
    if not shutil.which("ollama"):
        return None
    want = tag if ":" in tag else f"{tag}:latest"
    try:
        out = subprocess.run(["ollama", "list"], env=ollama_env(ollama_dir),
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return None
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if not parts or parts[0] != want:
            continue
        m = _OLLAMA_SIZE.search(line)
        if not m:
            return None
        return round(float(m.group(1)) * _SIZE_GB[m.group(2).lower()], 2)
    return None


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
    if not role:
        return None, None, None
    if role not in model_manifest:
        # The tier's preferred role is not declared. Ava ships no default chat
        # model, so on a big box that is the ORDINARY case, not a broken config —
        # returning None here made `pull --auto` a silent no-op on exactly the
        # hardware with the most room. Take any other declared role this box can
        # actually serve, and say that is what happened.
        for other, spec in model_manifest.items():
            if engine_servable_here(spec.get("engine")):
                return other, spec, (f"no '{role}' model is declared — pulling the "
                                     f"'{other}' role ({spec.get('engine')}: "
                                     f"{spec.get('id')}) instead")
        return None, None, (f"no '{role}' model is declared, and no other "
                            f"declared model can be served on {platform_label()}")
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
    case-sensitive about `mistralai/Mistral-7B-Instruct-v0.3`, and Ollama reports a pulled
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


#: What a config-vs-reality comparison can conclude. Closed, and mirrored in the
#: frontend (`BrainTruth` in lib/types.ts) — tests/test_model_state_vocabulary.py
#: reconciles the two.
#:
#: Only TWO of these are disagreements. The rest are the honest ways of not
#: knowing, and none of them may ever be reported as a disagreement: an engine
#: that is down, slow, or on someone else's box has not contradicted the config,
#: and a watchdog that pages on silence is a watchdog people turn off.
#:
#: `drifted` and `mismatched` are deliberately separate because they have
#: different consequences. `drifted` means the ENGINE answered and does not hold
#: the model — every turn fails, so it is critical. `mismatched` means two
#: CONFIG surfaces name different models; turns still succeed, because the router
#: rewrites the model id on the way through, but a UI reading the wrong surface
#: will name a model that is not answering. That is a lie, not an outage.
TRUTHS = ("agrees", "drifted", "mismatched", "unreachable", "unobservable",
          "elsewhere", "unconfigured")


def _configured_backend_model() -> str:
    """The model id of the backend Ava would route to, ignoring the sandbox.

    Only for the agent-branch comparison in `serving_truth`: it answers "what
    would serve a turn if the sandbox were not in the way", which is exactly what
    the sandbox's own upstream reaches. Returns "" when nothing is configured, so
    an install with no backend simply has nothing to disagree with.
    """
    try:
        from . import router_app

        backends = router_app.load_backends()
    except Exception:  # noqa: BLE001 — never let this break the resolver
        return ""
    if not backends:
        return ""
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
    return str(b.get("model") or "")


def serving_truth(brain: dict | None = None, *, served: list[str] | None = None,
                  reachable: bool | None = None, timeout: float = 2.0) -> dict:
    """What the engine is ACTUALLY serving, and whether it matches the config.

    The companion to `effective_brain()`, and deliberately a SECOND function
    rather than more fields on the first: that one is documented as reading only
    configuration, and an observed field on it would make its contract a lie.
    Two functions in one module — a caller that has one has the other — and the
    pair is what makes a disagreement expressible at all.

    That it was NOT expressible is the whole reason this exists. A router served
    a model id its engine did not have for twelve days; every completion 404'd;
    and the hardware panel showed a green, correctly-named brain the entire time,
    because the panel reads the engine, the router read the config, and no code
    path could hold the two side by side. `hardware._loaded_models` already
    computed this comparison inline and then threw the answer away.

    Pass `served`/`reachable` when you have already probed — `/api/hardware` is
    polled every 2s per open client and probes each local backend anyway, so the
    hot path must not probe a second time. With both supplied this does no I/O.
    """
    b = brain if brain is not None else effective_brain()
    out = {"verdict": "unconfigured", "want": "", "serving": [], "matched": "",
           "engine": "", "base_url": "", "backend_id": "", "source": "",
           "observed": False, "detail": ""}
    out.update(backend_id=str(b.get("backend_id") or ""),
               source=str(b.get("source") or ""),
               engine=str(b.get("engine") or ""),
               base_url=str(b.get("base_url") or ""),
               want=str(b.get("model_id") or ""))

    if out["source"] in ("", "none"):
        return {**out, "verdict": "unconfigured",
                "detail": "no brain is configured"}
    # The agent sandbox answers turns itself and holds its own model endpoint;
    # Ava does not have it. `effective_brain` says so where it sets local=True
    # with an empty base_url: unobservable rather than absent. Probing our own
    # router here would compare the config against an engine that is not the one
    # answering, which is a fabricated agreement or a fabricated drift.
    #
    # But there IS one comparison worth making, and missing it cost a real
    # discrepancy on 2026-08-13. The sandbox's model is baked in at
    # `nemoclaw onboard` time (NEMOCLAW_MODEL in its container), so swapping
    # Ava's backend leaves the sandbox naming the OLD model. Nothing failed —
    # the sandbox's upstream runs through Ava's router, which rewrites the model
    # id — but the hardware panel named a model that was no longer being served,
    # and put the engine that WAS serving under "Ava's other engines". Both ids
    # are config Ava can read, so the disagreement is observable even when the
    # engine behind the sandbox is not.
    if out["source"] == "agent":
        theirs = out["want"]
        ours = _configured_backend_model()
        if theirs and ours and theirs != ours:
            return {**out, "verdict": "mismatched", "serving": [ours],
                    "detail": (f"the agent sandbox is onboarded with {theirs}, but "
                               f"the model Ava serves is {ours}")}
        return {**out, "verdict": "unobservable",
                "detail": "the agent sandbox holds the model endpoint"}
    # A remote endpoint is not ours to poll on a timer — an API key would leave
    # the box every couple of seconds to learn something we cannot act on.
    if not b.get("local", False):
        return {**out, "verdict": "elsewhere",
                "detail": "the brain is on another host; not probed from here"}
    if not out["want"] or not out["base_url"]:
        return {**out, "verdict": "unconfigured",
                "detail": "no brain is configured"}

    if served is None or reachable is None:
        reachable, served = probe_serving(out["base_url"], out["engine"],
                                          str(b.get("api_key") or ""), timeout)
    out["observed"] = bool(reachable)
    out["serving"] = list(served or [])

    if not reachable:
        return {**out, "verdict": "unreachable",
                "detail": f"{out['base_url']} did not answer"}
    if not out["serving"]:
        # Reachable and listing nothing is a real state: a vLLM still loading
        # lists nothing, and an empty Ollama store lists nothing. Neither has
        # told us the configured model is absent.
        return {**out, "verdict": "unobservable",
                "detail": "the engine answered but listed no models"}

    matched = match_served(out["want"], out["serving"])
    if matched:
        # `matched` is the engine's OWN spelling, which can differ from the
        # config's (a bare Ollama tag against `llama3.2:latest`). Callers that
        # display an id should prefer it.
        return {**out, "verdict": "agrees", "matched": matched,
                "detail": f"serving {matched}"}
    return {**out, "verdict": "drifted",
            "detail": (f"configured for {out['want']}, but the engine is serving "
                       + ", ".join(out["serving"][:4]))}


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
    "serving_truth", "TRUTHS",
]


# --- the store as it is ON DISK ---------------------------------------------- #
#
# `manifest()` says what ava.yaml DECLARES. This says what is actually here,
# which is a different question and the one an owner asking "what can I run, and
# what is using my disk" is asking. A model pulled outside Ava, or pulled and
# since undeclared, is real, occupies real space, and was invisible to every
# surface until this existed — so it could not be selected and could not be
# reclaimed.
#
# SCOPED TO THE ENGINE DIRECTORIES, never the store root. `models/` also holds
# voice assets (`ecapa/`, `en_US-*.onnx`) which are not LLMs, must not be listed
# as models, and must never be offered for deletion by a model manager.

# HuggingFace caches under `<hf>/hub/models--<org>--<name>`, with a sibling
# `.locks/` tree that is bookkeeping, not weights.
_HF_SUBS = ("hub", "")

# What counts as a weight file in the gguf/ directory. Deliberately narrow: a
# README or a checksum sidecar living beside the weights is not a model, and
# listing it as one makes it deletable as one.
_WEIGHT_EXTS = (".gguf", ".bin", ".safetensors", ".pt", ".pth", ".onnx")


def _hf_id_from_dir(name: str) -> str:
    """`models--org--name` -> `org/name`. Returns "" for anything else.

    A repo id may itself contain no `--`, so only the leading marker is stripped
    and the remaining separators are restored in order.
    """
    if not name.startswith("models--"):
        return ""
    return name[len("models--"):].replace("--", "/")


def installed_models(model_dirs: dict | None = None) -> list[dict]:
    """Every model actually on disk, newest engines first, biggest first.

    Each row: {engine, id, size_gb, path}. `path` is what `delete_model` will
    remove and is always inside the engine's own directory — it is returned so a
    caller can show the owner exactly what is about to be deleted rather than
    asking them to trust a name.
    """
    d = model_dirs or dirs()
    out: list[dict] = []

    for sub in _HF_SUBS:
        base = os.path.join(d["hf"], sub) if sub else d["hf"]
        if not os.path.isdir(base):
            continue
        for entry in sorted(os.listdir(base)):
            mid = _hf_id_from_dir(entry)
            if not mid:
                continue
            p = os.path.join(base, entry)
            if not os.path.isdir(p):
                continue
            out.append({"engine": "vllm", "id": mid, "path": p,
                        "size_gb": _tree_gb(p)})

    if os.path.isdir(d["gguf"]):
        for entry in sorted(os.listdir(d["gguf"])):
            p = os.path.join(d["gguf"], entry)
            if os.path.isfile(p) and entry.lower().endswith(_WEIGHT_EXTS):
                out.append({"engine": "gguf", "id": entry, "path": p,
                            "size_gb": round(os.path.getsize(p) / (1024 ** 3), 2)})

    for tag, size in _ollama_installed(d["ollama"]):
        out.append({"engine": "ollama", "id": tag,
                    "path": d["ollama"], "size_gb": size})

    out.sort(key=lambda m: (m.get("size_gb") or 0), reverse=True)
    return out


def _ollama_installed(ollama_dir: str) -> list[tuple[str, float | None]]:
    """(tag, size_gb) for every pulled Ollama model, via `ollama list`.

    Asked of the daemon rather than read off disk: Ollama's store is
    content-addressed blobs plus manifests, so the directory says nothing about
    which tags exist and a tag's size is not any one file.
    """
    if not shutil.which("ollama"):
        return []
    try:
        out = subprocess.run(["ollama", "list"], env=ollama_env(ollama_dir),
                             capture_output=True, text=True, timeout=10)
    except Exception:  # noqa: BLE001
        return []
    rows = []
    for line in out.stdout.splitlines()[1:]:
        parts = line.split()
        if not parts:
            continue
        m = _OLLAMA_SIZE.search(line)
        rows.append((parts[0],
                     round(float(m.group(1)) * _SIZE_GB[m.group(2).lower()], 2) if m else None))
    return rows


class ModelDeleteError(RuntimeError):
    """A refusal, with an owner-readable reason. Never raised for 'not found'."""


def _contained(path: str, root: str) -> bool:
    """Is `path` genuinely inside `root`, after following every symlink?

    The containment check is the whole safety story for a function that runs
    `rmtree`. Comparing the strings as given is not enough: a symlinked store
    directory, or an id carrying `../`, resolves somewhere else entirely, and
    the caller-supplied id reaches this function from an HTTP route.
    """
    rp, rr = os.path.realpath(path), os.path.realpath(root)
    return rp == rr or rp.startswith(rr + os.sep)


def delete_model(engine: str, model_id: str, model_dirs: dict | None = None,
                 *, forced: bool = False, held_by: list[str] | None = None) -> dict:
    """Remove a model's weights from disk. Returns {removed, freed_gb, paths}.

    Reclaims the space for real — the point of the operation. Deleting the
    manifest entry alone left tens of GB on the volume with nothing in any UI
    pointing at it.

    Refuses rather than guesses:
      * an id that does not resolve inside the engine's own store directory
        raises, because the only ways to get there are a traversal attempt and a
        bug, and both should stop at the boundary rather than at `rmtree`;
      * Ollama is deleted through `ollama rm`, never by removing files. Its
        store is content-addressed blobs shared BETWEEN tags, so unlinking a
        manifest's blobs corrupts every other model that shares a layer.

    Whether the model is in USE is not decided here — `hub/models.py` owns that,
    because it is the layer that knows about backends and roles.
    """
    d = model_dirs or dirs()
    eng = (engine or "").strip().lower()
    mid = (model_id or "").strip()
    if not mid:
        raise ModelDeleteError("no model id given")

    if eng == "ollama":
        if not shutil.which("ollama"):
            raise ModelDeleteError("the ollama command is not on PATH here")
        try:
            r = subprocess.run(["ollama", "rm", mid], env=ollama_env(d["ollama"]),
                               capture_output=True, text=True, timeout=60)
        except Exception as e:
            raise ModelDeleteError(f"ollama rm failed: {e}") from e
        if r.returncode != 0:
            err = (r.stderr or r.stdout or "").strip()[:200]
            if "not found" in err.lower():
                return {"removed": False, "freed_gb": 0.0, "paths": []}
            raise ModelDeleteError(err or "ollama rm failed")
        _audit_delete("ollama", mid, None, forced, held_by)
        return {"removed": True, "freed_gb": None, "paths": [d["ollama"]]}

    if eng == "vllm":
        targets = []
        for sub in _HF_SUBS:
            base = os.path.join(d["hf"], sub) if sub else d["hf"]
            safe = "models--" + mid.replace("/", "--")
            for cand in (os.path.join(base, safe),
                         os.path.join(base, ".locks", safe)):
                if os.path.exists(cand):
                    if not _contained(cand, d["hf"]):
                        raise ModelDeleteError(
                            f"refusing to delete {cand}: outside the model store")
                    targets.append(cand)
        return _remove_paths(targets, "vllm", mid, forced, held_by)

    if eng in ("gguf", "llamacpp"):
        p = gguf_path({"id": mid}, d["gguf"])
        if not os.path.exists(p):
            return {"removed": False, "freed_gb": 0.0, "paths": []}
        if not _contained(p, d["gguf"]):
            raise ModelDeleteError(f"refusing to delete {p}: outside the model store")
        return _remove_paths([p], eng, mid, forced, held_by)

    raise ModelDeleteError(f"unknown engine '{engine}'")


def _audit_delete(engine: str, model_id: str, freed_gb, forced: bool,
                  held_by: list[str] | None) -> None:
    """Record the destruction where it happens (tests/test_destructive_paths_audited).

    Never fatal: the weights are already gone by the time this runs, and a
    ledger write that raises would turn a completed deletion into a 500 the
    owner would reasonably retry.
    """
    try:
        from . import audit
        audit.record("model_delete", engine=engine, model=model_id,
                     freed_gb=freed_gb, forced=bool(forced),
                     held_by=list(held_by or []))
    except Exception:  # noqa: BLE001
        pass


def _remove_paths(paths: list[str], engine: str, model_id: str,
                  forced: bool = False, held_by: list[str] | None = None) -> dict:
    """Delete each path, measuring first so the owner is told what was freed."""
    if not paths:
        return {"removed": False, "freed_gb": 0.0, "paths": []}
    freed = 0.0
    for p in paths:
        freed += (_tree_gb(p) if os.path.isdir(p)
                  else round(os.path.getsize(p) / (1024 ** 3), 2)) or 0.0
    for p in paths:
        if os.path.isdir(p):
            shutil.rmtree(p, ignore_errors=True)
        else:
            try:
                os.remove(p)
            except OSError:
                pass
    _audit_delete(engine, model_id, round(freed, 2), forced, held_by)
    return {"removed": True, "freed_gb": round(freed, 2), "paths": paths}
