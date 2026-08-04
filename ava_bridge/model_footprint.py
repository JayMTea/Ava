"""What a model actually costs to run on THIS box, and how sure we are.

`model_fit` has always been able to answer "would a model of size N fit" — but
`N` was hand-declared per backend in `ava.yaml`, and nothing derived it from the
model. So the fit machinery existed and the number feeding it did not, and the
model picker fell back to the coarse hardware tier instead. A tier is a proxy
that is wrong at the boundaries: a 7B at Q4 is about 5 GB, the same 7B at FP16 is
three times that before any KV cache. Same tier, different answer.

**The trust ladder, and why it is a ladder rather than a formula.**

    observed   this box ran this model and the engine reported what it took.
               Not an estimate. Ollama's /api/ps gives the resident size AND the
               VRAM/RAM split, so it also says whether it fitted on the
               accelerator or spilled.
    declared   the owner wrote `fit.weight_gb` in ava.yaml. Their box, their
               number.
    derived    computed: weights from the engine's on-disk size, KV cache from
               the model's own architecture at the context it will run.
    unknown    nothing resolvable. Say so. Do not guess.

**The asymmetry that governs every caller.** "Will not fit" is arithmetic —
weights alone exceeding the pool is a fact. "Will fit" is never more than a
projection: allocator fragmentation, a second model holding a lease, and WSL2's
default swap all sit between an estimate and reality. So a confident "fits!"
that then thrashes is worse than the vague tier it replaced, because it converts
the owner's caution into trust. Callers must be blunt in the first direction and
hedged in the second — see `model_fit.verdict`.

**The failure this exists to catch is not usually a kill.** A model that
overflows the accelerator does not crash; it spills to CPU and runs 5-30x
slower, silently, and the owner concludes Ava is bad. That is the common case,
it is invisible today, and — uniquely — the engine can be asked about it after
the fact rather than predicted.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from . import engines as _engines

OBSERVED, DECLARED, DERIVED, UNKNOWN = "observed", "declared", "derived", "unknown"

_GIB = 1024 ** 3
# KV cache entries are float16 unless an engine is told otherwise. Ollama and
# llama.cpp both default to f16; a q8_0 KV halves this and is opt-in, so
# assuming f16 errs toward reporting a LARGER footprint, which is the safe
# direction for a fit check.
_KV_BYTES_PER_ELEM = 2
# What the engine itself occupies beyond weights and KV — CUDA/Metal context,
# activation scratch, the runtime. A flat allowance rather than a model of it:
# a fake-precise number here would imply we measured something we did not.
_ENGINE_OVERHEAD_GB = 0.8


@dataclass(frozen=True)
class Footprint:
    """What running `model` costs, and where that figure came from."""
    model: str
    source: str                      # observed | declared | derived | unknown
    detail: str                      # provenance, for the owner not the log
    weight_gb: float | None = None
    kv_gb: float | None = None
    context: int | None = None
    overhead_gb: float = 0.0
    # observed only: did it actually sit on the accelerator, or spill to CPU?
    # None everywhere else — "we did not look" is not "it was fine".
    spilled: bool | None = None

    @property
    def total_gb(self) -> float | None:
        if self.weight_gb is None:
            return None
        return round(self.weight_gb + (self.kv_gb or 0.0) + self.overhead_gb, 2)

    @property
    def measured(self) -> bool:
        return self.source == OBSERVED


def kv_cache_gb(arch: dict, context: int,
                bytes_per_elem: int = _KV_BYTES_PER_ELEM) -> float | None:
    """KV cache size for `context` tokens, from a model's own architecture.

    2 (one K and one V) x layers x kv_heads x head_dim x tokens x bytes.

    `head_dim` is derived as embedding_length / head_count, and `kv_heads` is
    head_count_kv rather than head_count — grouped-query attention makes those
    differ by up to 8x on modern models, and using the wrong one overstates the
    cache badly enough to recommend against a model that fits comfortably.
    """
    try:
        layers = int(arch["layers"])
        heads = int(arch["heads"])
        kv_heads = int(arch.get("kv_heads") or heads)
        embed = int(arch["embedding"])
    except (KeyError, TypeError, ValueError):
        return None
    if min(layers, heads, kv_heads, embed) <= 0 or context <= 0:
        return None
    head_dim = embed / heads
    return round(2 * layers * kv_heads * head_dim * context
                 * bytes_per_elem / _GIB, 2)


def architecture(base_url: str, model: str, engine: str = "",
                 timeout: float = 4.0) -> dict | None:
    """A model's shape, from the engine holding it. None if it cannot be asked.

    Ollama namespaces these keys by family (`llama.block_count`,
    `qwen2.block_count`, …), so they are matched by SUFFIX rather than by a
    per-family table that would go stale the week a new architecture lands.
    """
    e = _engines.get(engine)
    if e is None or not e.arch_path:
        return None
    base = (base_url or "").rstrip("/")
    if base.endswith("/v1"):
        base = base[:-3].rstrip("/")
    try:
        import requests
        r = requests.post(base + e.arch_path, json={"model": model},
                          timeout=timeout)
        if not r.ok:
            return None
        info = (r.json() or {}).get("model_info") or {}
    except Exception:  # noqa: BLE001 — unreachable, slow, or not JSON
        return None
    if not isinstance(info, dict):
        return None

    def _find(suffix: str):
        for k, v in info.items():
            if isinstance(k, str) and k.endswith(suffix):
                return v
        return None

    out = {"layers": _find(".block_count"),
           "heads": _find(".attention.head_count"),
           "kv_heads": _find(".attention.head_count_kv"),
           "embedding": _find(".embedding_length"),
           "train_context": _find(".context_length")}
    return out if out["layers"] and out["embedding"] and out["heads"] else None


def observed(base_url: str, engine: str = "") -> dict[str, Footprint]:
    """What the engine says it is holding RIGHT NOW, keyed by model id.

    The only rung of the ladder that is a measurement. Ollama reports both the
    total resident size and how much of it is on the accelerator, so this is
    also the one place the spill can be observed rather than predicted.

    `size` is the WHOLE resident footprint — weights, KV cache and engine
    overhead already in it. So `kv_gb` and `overhead_gb` stay zero here on
    purpose: adding a computed cache on top of a measurement that contains one
    would double-count it and reject models that fit. This is why `Footprint`
    keeps the parts separate rather than only carrying a total.
    """
    from . import models as _models
    rows = _models.probe_resident(base_url, engine)
    if not rows:
        return {}
    out: dict[str, Footprint] = {}
    for r in rows:
        size = r.get("size_bytes")
        if not size:
            continue
        vram = r.get("vram_bytes")
        # `size_vram` is omitted rather than zeroed on some builds, so absent
        # means "we cannot tell", not "none of it was on the GPU".
        spilled = None if vram is None else bool(vram < size)
        out[r["id"]] = Footprint(
            model=r["id"], source=OBSERVED,
            detail="measured while this model was loaded here",
            weight_gb=round(size / _GIB, 2), spilled=spilled)
    return out


def declared(model: str) -> Footprint | None:
    """A weight the owner wrote down for this model in ava.yaml."""
    try:
        from . import settings
        backends = settings.get("inference.backends", {}) or {}
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(backends, dict):
        return None
    for bid, spec in backends.items():
        if not isinstance(spec, dict):
            continue
        if str(spec.get("model", "")).strip() != model:
            continue
        w = ((spec.get("fit") or {}) if isinstance(spec.get("fit"), dict) else {}
             ).get("weight_gb")
        try:
            w = float(w)
        except (TypeError, ValueError):
            continue
        if w > 0:
            return Footprint(model=model, source=DECLARED,
                             detail=f"declared for backend `{bid}` in ava.yaml",
                             weight_gb=round(w, 2))
    return None


def derived(base_url: str, model: str, engine: str = "",
            context: int | None = None,
            disk_gb: float | None = None) -> Footprint | None:
    """Weights from the engine's inventory, KV from the model's architecture.

    Returns a partial footprint when only the weights resolve — half an answer
    that says which half is missing beats no answer, because "weights alone
    exceed your pool" is still decidable from it.
    """
    if disk_gb is None:
        disk_gb = _disk_gb(base_url, model, engine)
    if disk_gb is None:
        return None
    arch = architecture(base_url, model, engine)
    kv = ctx = None
    if arch:
        ctx = int(context or arch.get("train_context") or 0) or None
        if ctx:
            kv = kv_cache_gb(arch, ctx)
    detail = ("size on disk" if kv is None
              else f"size on disk + KV cache at {ctx:,} tokens")
    return Footprint(model=model, source=DERIVED, detail=detail,
                     weight_gb=round(disk_gb, 2), kv_gb=kv, context=ctx,
                     overhead_gb=_ENGINE_OVERHEAD_GB)


def _disk_gb(base_url: str, model: str, engine: str = "",
             timeout: float = 4.0) -> float | None:
    """On-disk size of a pulled model. Close enough to its weight footprint:
    a GGUF is memory-mapped essentially as-is, and a safetensors checkpoint
    loads to about its file size."""
    e = _engines.get(engine)
    if e is None or e.models_path != "/api/tags":
        return None                       # only Ollama reports per-model sizes
    from . import models as _models
    url = _models.models_url(base_url, engine)
    if not url:
        return None
    try:
        import requests
        r = requests.get(url, timeout=timeout)
        if not r.ok:
            return None
        items = (r.json() or {}).get("models") or []
    except Exception:  # noqa: BLE001
        return None
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or it.get("model") or "")
        if name == model or name.split(":")[0] == model.split(":")[0]:
            try:
                return float(it["size"]) / _GIB
            except (KeyError, TypeError, ValueError):
                return None
    return None


def resolve(model: str, base_url: str = "", engine: str = "",
            context: int | None = None) -> Footprint:
    """The best footprint available for `model`, highest rung first.

    Never raises and never returns None: a caller asking "what does this cost"
    always gets an answer, and `source == UNKNOWN` is one of the legitimate
    ones. An exception here would take down a model list over a footprint.
    """
    model = (model or "").strip()
    if not model:
        return Footprint(model="", source=UNKNOWN, detail="no model named")
    try:
        from . import footprint_store
        remembered = footprint_store.get(model)
        if remembered is not None:
            return remembered
    except Exception:  # noqa: BLE001 — a cache miss must never be fatal
        pass
    if base_url:
        try:
            live = observed(base_url, engine).get(model)
            if live is not None:
                return live
        except Exception:  # noqa: BLE001
            pass
    try:
        d = declared(model)
        if d is not None:
            return d
    except Exception:  # noqa: BLE001
        pass
    if base_url:
        try:
            got = derived(base_url, model, engine, context)
            if got is not None:
                return got
        except Exception:  # noqa: BLE001
            pass
    return Footprint(model=model, source=UNKNOWN,
                     detail="this engine cannot be asked what the model costs")


def default_context() -> int:
    """The context a fit check should assume when nobody has said otherwise.

    Ava's own system prompt and tool schemas occupy ~29k tokens, which is why
    deploy/model-flags.conf resolves 32768 rather than something smaller — a
    shorter context breaks the agent before it saves any memory. Checking fit at
    a context the install will never run at would be checking the wrong thing.
    """
    try:
        return max(1, int(os.environ.get("AVA_FIT_CONTEXT", "32768")))
    except ValueError:
        return 32768
