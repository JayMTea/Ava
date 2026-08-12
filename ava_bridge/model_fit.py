"""Ava model-fitting: connect live hardware to inference routing.

The router (`ava_router.py`) can fail over between already-running backends, but
it has no idea *what each model is for* or *whether the box has room to run it
right now*. This module is the missing brain between the hardware monitor and the
router. It answers two questions:

    1. select(workload) -> which backend should serve THIS kind of work, given
       the model's strengths AND the current memory pressure on the box.
    2. fits(weight_gb) -> would a model of this size fit in the free memory pool
       right now (so a future loader / the dashboard can decide before trying).

**What "free memory" means adapts to the hardware it runs on:**

  - **Discrete NVIDIA GPU** (most installs): the model lives in VRAM, so the
    fit-limiting resource is *free VRAM* — read from `nvidia-smi memory.free`.
  - **Unified memory** (the dev target — DGX Spark GB10 / Grace-Blackwell): CPU
    and GPU share ~121 GiB and `nvidia-smi` reports VRAM as N/A, so we fall back
    to `MemAvailable` from /proc/meminfo (the one real pool a second concurrent
    model eats into).
  - **No GPU / non-Linux / cloud-only**: neither source resolves, so `free_mem_gb`
    returns None and callers treat memory as "unknown, don't gate" — routing
    degrades to plain primary-first, never worse than before this layer.

Both hardware reads are TTL-cached so ordering every request stays ~free (one
`nvidia-smi` spawn every few seconds at most, not per request).

**Remote/cloud backends are never memory-gated.** A backend that isn't local
(OpenAI, OpenRouter, a remote host) doesn't consume this box's memory, so its
`weight_gb`/`min_free_gb` are ignored and it always "fits" — the memory guard
only sheds *local* engines.

**Scope.** This selects and fit-checks; it does not launch, size, or quantize
engines (nothing in this repo does — the vLLM servers are pre-started). It layers
on top of the router's existing manual toggle + error failover, which remain the
safety net when a fit-preferred backend is actually down.

Each backend declares its fit profile in ava.yaml under `inference.backends.<id>.fit`:

    fit:
      weight_gb: 71              # resident memory the loaded engine needs
      workloads: [chat, reasoning, code]   # what this model is good at
      tier: large                # large | small | tiny (quality/size class)
      min_free_gb: 6             # headroom to keep free while it serves

Backends with no `fit:` block still work — they get a permissive default profile
so an unconfigured install behaves exactly as before.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

from . import hwinfo

# --- live memory read (the fit-limiting resource) ---------------------------- #
# All hardware knowledge lives in the HAL (ava_bridge/hwinfo.py): it picks free
# VRAM on a discrete GPU, the unified system pool on Spark/Apple Silicon, or None
# when nothing is readable. This layer just asks it and decides routing.


def free_mem_gb() -> float | None:
    """Free fit-limiting memory in GiB (HAL-resolved, hardware-adaptive).

    Returns None when the HAL can read no memory source (e.g. non-Linux without
    psutil) so callers treat memory as unknown and never block routing on it.
    """
    return hwinfo.fit_memory().free_gb


def mem_source() -> str | None:
    """Where free_mem_gb() came from: 'vram-*' | 'system-*' | None (see hwinfo)."""
    return hwinfo.fit_memory().source


# --- model profiles ---------------------------------------------------------- #
# Tier preference per workload: which size class we *want* first when several
# backends can serve a workload and the box has room. Chat/reasoning/code favour
# the big brain; fast/cheap/utility favour the small one; anything unlisted is
# treated as a general workload that prefers quality when memory allows.
_WORKLOAD_TIER_PREF = {
    "chat": ["large", "small", "tiny"],
    "reasoning": ["large", "small", "tiny"],
    "code": ["large", "small", "tiny"],
    "vision": ["large", "small", "tiny"],
    "fast": ["small", "tiny", "large"],
    "cheap": ["small", "tiny", "large"],
    "utility": ["small", "tiny", "large"],
    "classify": ["small", "tiny", "large"],
}
_DEFAULT_TIER_PREF = ["large", "small", "tiny"]
_TIER_RANK = {"large": 0, "small": 1, "tiny": 2}


# Engines that run ON this box (consume its memory) vs cloud APIs that don't.
# Includes the Apple-Silicon-friendly local servers (Ollama, llama.cpp, LM Studio,
# MLX) so a Mac install is detected as local and gets memory-gated correctly.
# `gguf` was missing, and it is a real engine value: models.present() treats it as
# a llama.cpp alias and models.LOCAL_CHAT_ENGINES lists it. Absent here, a backend
# declared `engine: gguf` counted as CLOUD — so a local GGUF model consuming this
# box's memory was never memory-gated, which is the "wrong there is room" failure
# tests/test_alloc_measurement.py's docstring warns about, one layer up.
# `tgi`/`sglang`/`local` are not in the engine registry (Ava neither launches nor
# health-checks them) but they DO run locally, and being conservative about what
# consumes memory is the safe direction.
_LOCAL_ENGINES = {"vllm", "ollama", "llamacpp", "llama.cpp", "llamafile", "gguf",
                  "tgi", "sglang", "mlx", "mlx-lm", "lmstudio", "lm-studio",
                  "local"}
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0", "host.docker.internal"}


def is_local(backend: dict) -> bool:
    """True if this backend runs on this machine (so its memory footprint counts).

    A cloud/remote endpoint (OpenAI, OpenRouter, a LAN box) doesn't consume this
    host's memory, so it must never be memory-gated. Decided by an explicit
    `fit.local`, then engine type, then a loopback base_url.

    Public because it is the deployment's ONE local-vs-cloud decision. Three
    other modules had grown their own hostname set, and hardware.py's — four
    loopback literals — dropped `http://ollama:11434/v1`, the URL every Docker
    install is given, so the monitor could not see the brain it was serving.
    A compose service name is not a cloud endpoint; the engine kind says so.
    """
    fit = backend.get("fit") or {}
    if "local" in fit:
        return bool(fit["local"])
    eng = str(backend.get("engine", "")).strip().lower()
    if eng in _LOCAL_ENGINES:
        return True
    # A loopback base_url is direct evidence and outranks the key heuristic.
    # A Bearer token usually means a cloud API, but llama-server and vLLM both
    # take `--api-key`, and the shipped "Cloud (OpenAI-compatible)" preset is
    # the only add-a-model form with a key field — so a token-protected engine
    # on 127.0.0.1 arrives here looking exactly like OpenAI while holding this
    # box's RAM. Tested first, it stays local; tested second, the monitor calls
    # it "runs elsewhere" and the locality filter drops the row entirely.
    host = (urlparse(backend.get("url", "")).hostname or "").lower()
    if host in _LOCAL_HOSTS:
        return True
    return False        # no local signal left; a Bearer key means a cloud API


@dataclass
class FitProfile:
    """The fit metadata for one backend (from ava.yaml `...fit`, with defaults)."""
    backend_id: str
    weight_gb: float | None = None          # resident footprint; None = unknown
    workloads: frozenset = field(default_factory=frozenset)
    tier: str = "large"                     # large | small | tiny
    min_free_gb: float = 4.0                # headroom to keep free while serving
    local: bool = True                      # runs on this box? remote => not gated

    def handles(self, workload: str) -> bool:
        """True if this model is declared good at `workload` (or tags nothing)."""
        return (not self.workloads) or (workload in self.workloads)


def profile_for(backend: dict) -> FitProfile:
    """Build a FitProfile from a router backend dict's optional `fit` block.

    `backend` is the shape ava_router builds: {id, url, model, label, engine,
    api_key, fit?}. Missing/partial `fit` yields a permissive default so an
    unconfigured backend still routes.
    """
    fit = backend.get("fit") or {}
    workloads = fit.get("workloads") or []
    if isinstance(workloads, str):
        workloads = [workloads]
    tier = str(fit.get("tier", "large")).strip().lower()
    if tier not in _TIER_RANK:
        tier = "large"
    return FitProfile(
        backend_id=backend.get("id", "?"),
        weight_gb=_as_float(fit.get("weight_gb")),
        workloads=frozenset(str(w).strip().lower() for w in workloads if w),
        tier=tier,
        min_free_gb=_as_float(fit.get("min_free_gb")) or 4.0,
        local=is_local(backend),
    )


def _as_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --- the fit decision -------------------------------------------------------- #
@dataclass
class Candidate:
    """One backend scored for a request, with a human-readable reason."""
    backend: dict
    profile: FitProfile
    fits: bool          # will the box hold it right now?
    handles: bool       # is the model suited to this workload?
    score: float        # lower = try first
    reason: str


def fits_now(profile: FitProfile, free_gb: float | None,
             assume_loaded: bool = True) -> tuple[bool, str]:
    """Would this model be safe to serve given the free memory pool right now?

    `assume_loaded=True` (the router's case: engines are pre-started) checks only
    that there's `min_free_gb` of working headroom left — enough to serve without
    tipping the box into OOM under concurrent load. `assume_loaded=False` (a
    would-be loader / dashboard "can I start this?") also requires `weight_gb` of
    free room to bring the engine up.

    A remote backend (`profile.local` False) never consumes this host's memory,
    so it always fits regardless of the local pool.
    """
    if not profile.local:
        return True, "remote backend — not memory-gated"
    if free_gb is None:
        return True, "memory unknown — not gated"
    need = profile.min_free_gb
    if not assume_loaded and profile.weight_gb:
        need += profile.weight_gb
    if free_gb >= need:
        return True, f"{free_gb:.0f}GiB free ≥ {need:.0f}GiB needed"
    return False, f"{free_gb:.0f}GiB free < {need:.0f}GiB needed"


def select(backends: list, workload: str | None = None,
           primary_id: str | None = None,
           free_gb: float | None = None) -> list[Candidate]:
    """Order backends best-first for a request.

    Ranking (each a tie-breaker for the previous):
      1. **Fits now** — a backend the box can't safely serve is sorted last, so
         under memory pressure we shed to a lighter model instead of OOM-ing.
      2. **Handles the workload** — a model tagged for this kind of work first.
      3. **Tier preference** for the workload (big brain for chat/code, small for
         fast/cheap), so work lands on the right-sized model.
      4. **User's manual primary** — the explicit `_mode` pick breaks remaining ties.

    Returns Candidates in try-order. The router forwards to the first that's
    actually reachable, so this composes with the existing error failover: a
    fit-preferred backend that's down still falls through to the next.
    """
    if free_gb is None:
        free_gb = free_mem_gb()
    wl = (workload or "").strip().lower() or None
    tier_pref = _WORKLOAD_TIER_PREF.get(wl, _DEFAULT_TIER_PREF) if wl else _DEFAULT_TIER_PREF

    cands: list[Candidate] = []
    for b in backends:
        prof = profile_for(b)
        ok, mem_reason = fits_now(prof, free_gb)
        handles = prof.handles(wl) if wl else True
        tier_score = tier_pref.index(prof.tier) if prof.tier in tier_pref else len(tier_pref)
        is_primary = 0 if b.get("id") == primary_id else 1
        # Composite sort key: fit dominates, then workload match, then tier, then
        # the manual primary. Encoded as one tuple-flattened float-free score via
        # a tuple stored alongside for a stable sort.
        cands.append(Candidate(
            backend=b, profile=prof, fits=ok, handles=handles,
            score=0.0,  # real ordering is the tuple below
            reason=_reason(wl, prof, ok, mem_reason, handles),
        ))
        cands[-1]._key = (0 if ok else 1, 0 if handles else 1, tier_score, is_primary)

    cands.sort(key=lambda c: c._key)
    return cands


def _reason(wl, prof: FitProfile, fits: bool, mem_reason: str, handles: bool) -> str:
    bits = [f"tier={prof.tier}"]
    if wl:
        bits.append(f"{'handles' if handles else 'off-workload for'} {wl}")
    bits.append(mem_reason if fits else f"SHED: {mem_reason}")
    return "; ".join(bits)


def fit_report(backends: list) -> dict:
    """Live fit snapshot for the dashboard / CLI: for each backend, does it fit,
    what's it for, and how much headroom is left. Read-only — decides nothing.
    """
    mem = hwinfo.fit_memory()
    free_gb = mem.free_gb
    rows = []
    for b in backends:
        prof = profile_for(b)
        loaded_ok, loaded_reason = fits_now(prof, free_gb, assume_loaded=True)
        cold_ok, cold_reason = fits_now(prof, free_gb, assume_loaded=False)
        rows.append({
            "id": prof.backend_id,
            "label": b.get("label") or prof.backend_id,
            "tier": prof.tier,
            "local": prof.local,
            "workloads": sorted(prof.workloads),
            "weight_gb": prof.weight_gb,
            "min_free_gb": prof.min_free_gb,
            "serve_ok": loaded_ok, "serve_reason": loaded_reason,
            "cold_load_ok": cold_ok, "cold_load_reason": cold_reason,
        })
    return {"free_gb": round(free_gb, 1) if free_gb is not None else None,
            "total_gb": round(mem.total_gb, 1) if mem.total_gb else None,
            "mem_source": mem.source,   # 'vram-*' | 'system-*' | None
            "gating": "enabled" if mem.readable else "disabled",
            "platform": hwinfo.platform_id(),
            "backends": rows, "ts": time.time()}


def recommend_tier(avail_gb: float) -> tuple:
    """Map available fit-memory (GB) to a concrete model class + example, so a
    user knows what will run on their hardware without guessing. Single source
    of truth for `ava doctor`, `ava models pull --auto` and the setup wizard."""
    # Name models by CLASS with a widely-available example, never "the default".
    # This line used to advertise the maintainer's own 30B checkpoint as the
    # default. Ava now ships no default model at all, which makes the rule
    # simpler: these are examples of what a tier can HOLD, never a
    # recommendation of what to run.
    if avail_gb >= 40:
        return "large", "~30B-class models (e.g. Mixtral 8x7B, Gemma 2 27B)"
    if avail_gb >= 20:
        return "medium", "~13-14B models (e.g. Mistral Nemo 12B), or a quantized 32B"
    if avail_gb >= 12:
        return "small", "~7-8B models (e.g. Llama 3.1 8B, Gemma 2 9B)"
    if avail_gb >= 6:
        return "tiny", "~3-7B quantized (Q4) models via Ollama"
    return "cloud", "too little local memory — use a hosted API (cloud profile)"


# --- will this model actually RUN well here? --------------------------------- #
#
# A distinct question from `fits_now`, which asks whether a CONFIGURED BACKEND is
# safe to route to right now. This asks whether a model in the store is worth
# downloading and running at all, and its answer is shown per row in
# Setup -> Agent -> Brain. The frontend words it (frontend/src/lib/modelFit.ts);
# everything here is a fact or a machine token.

# Closed vocabulary. `frontend/src/lib/modelFit.ts` mirrors it and
# tests/test_fit_vocabulary.py fails if the two drift — the frontend renders an
# unknown token as silence, so a seventh verdict would go quiet, not wrong.
FIT_VERDICTS = ("should_fit", "wont_fit", "will_spill", "unknown")

# How far past the pool a model can go before it stops being "slow" and starts
# being pointless. Spilling a little means paging some layers through system RAM
# on every token — many times slower, but it runs. At more than this multiple of
# the pool almost nothing is resident and it is not a usable model, it is a
# progress bar. A ratio rather than an absolute, because the point at which
# spilling stops being worth it scales with the box.
_SPILL_LIMIT = 2.0

# Headroom to leave for the KV cache, activations and the rest of the box. Scaled
# for small pools so a 6 GB box is not told that nothing fits: a fixed 4 GB is a
# rounding error on 128 GB and two thirds of the budget on 6.
_RESERVE_FRACTION = 0.15
_RESERVE_MAX_GB = 4.0

# Engines whose tags are quantised unless the name says otherwise. Used only by
# `size_from_id`'s last-resort estimate, never by anything that measures.
_QUANTISED_ENGINES = frozenset({"ollama", "llamacpp", "gguf", "lmstudio", "lm-studio"})


def _reserve_gb(pool_gb: float) -> float:
    return min(_RESERVE_MAX_GB, pool_gb * _RESERVE_FRACTION)


def assess(need_gb: float | None, pool_gb: float | None, pool_kind: str | None,
           spilled: bool | None = None) -> dict:
    """Will a model of `need_gb` run well in a `pool_gb` pool?

    Returns the row's `fit` block. `verdict` is always one of FIT_VERDICTS.

    `spilled` is an OBSERVATION — the engine reported part of this model sitting
    outside the accelerator last time it ran. When present it outranks the
    arithmetic, because it is a measurement of the thing the arithmetic is only
    predicting, and it is reported as `measured` so the UI can say "ran" instead
    of "likely to".

    SPILL IS ONLY MEANINGFUL ON A DISCRETE ACCELERATOR. On a unified pool
    (DGX Spark, Apple Silicon) there is one pool and nothing to spill INTO — a
    model either fits in the machine's memory or it does not. Reporting "runs
    partly on the processor" there would describe a boundary that does not
    exist, so a unified box only ever gets `should_fit` or `wont_fit`.
    """
    unified = (pool_kind or "").strip().lower() in ("unified", "system")
    # Rounded once, here, so every consumer sees the same figure. `fit_memory()`
    # reports the pool to full float precision (121.68934631347656 on a Spark),
    # and a payload that ships that is inviting each reader to round it its own
    # way — which is how one surface says 121.7 GB and the next says 121.69.
    if pool_gb is not None:
        pool_gb = round(pool_gb, 1)
    if need_gb is not None:
        need_gb = round(need_gb, 1)

    if spilled and not unified:
        return {"verdict": "will_spill", "source": "observed", "detail": "",
                "need_gb": need_gb, "pool_gb": pool_gb, "pool_kind": pool_kind,
                "measured": True, "spilled": True,
                "headroom_gb": (round(pool_gb - need_gb, 1)
                                if need_gb is not None and pool_gb is not None else None)}

    # "We could not read a size" is its own answer, and the UI renders it as
    # silence. Guessing here is what a fit advisory must never do: a wrong
    # "too large" on a model that runs fine costs the owner the model.
    if need_gb is None or pool_gb is None or pool_gb <= 0:
        return {"verdict": "unknown", "source": "derived", "detail": "",
                "need_gb": need_gb, "pool_gb": pool_gb, "pool_kind": pool_kind,
                "measured": False, "spilled": None, "headroom_gb": None}

    headroom = round(pool_gb - need_gb, 1)
    base = {"source": "derived", "detail": "", "need_gb": need_gb,
            "pool_gb": pool_gb, "pool_kind": pool_kind, "measured": False,
            "spilled": None, "headroom_gb": headroom}

    if need_gb > pool_gb * _SPILL_LIMIT:
        return {**base, "verdict": "wont_fit"}
    if need_gb > pool_gb - _reserve_gb(pool_gb):
        # Past the pool but within reach of spilling. On a unified box there is
        # nowhere to spill to, so the same arithmetic reads as "won't fit".
        return {**base, "verdict": "wont_fit" if unified else "will_spill"}
    return {**base, "verdict": "should_fit"}


def size_from_id(model_id: str | None, engine: str | None = None) -> float | None:
    """A rough GB footprint read out of a model's NAME, or None.

    Deliberately last-resort, and deliberately crude. `models.disk_size_gb`
    measures the real thing and is used whenever the weights are here; this
    exists only so a model the owner has NOT downloaded yet can still be flagged
    as far too large before they spend an hour pulling it.

    Reads two facts a model id conventionally carries and nothing else: the
    parameter count (`70b`, `8x7b`) and a quantisation hint. No table of known
    model names — a fork running anything gets the same treatment, which is the
    same rule `_short_model_name` in hardware.py follows.

    Two places it knowingly errs high, both toward warning rather than missing:
    an MoE name multiplies out (`8x7b` -> 56B where Mixtral is 46.7B total), and
    a name with no quantisation hint is read at its ecosystem's default rather
    than its smallest. A fit advisory that misses a model too big for the box
    costs the owner an hour and a disk; one that over-warns costs a second look.

    Returns None when the name says nothing, because a guess with no evidence
    behind it is what the `unknown` verdict is for.
    """
    s = (model_id or "").lower()
    m = re.search(r"(?<![\d.])(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*b\b", s)
    if m:
        billions = float(m.group(1)) * float(m.group(2))
    else:
        m = re.search(r"(?<![\d.])(\d+(?:\.\d+)?)\s*b\b", s)
        if not m:
            return None
        billions = float(m.group(1))
    if billions <= 0 or billions > 2000:      # not a parameter count
        return None
    # Bytes per parameter, from the precision the id names.
    if re.search(r"\b(q2|q3|int3)\b|-q2|-q3", s):
        per = 0.45
    elif re.search(r"\b(q4|int4|awq|gptq|nf4)\b|-q4", s):
        per = 0.60
    elif re.search(r"\b(q5|q6)\b|-q5|-q6", s):
        per = 0.85
    elif re.search(r"\b(q8|int8|fp8)\b|-q8", s):
        per = 1.10
    else:
        # Nothing named, so fall back to what THIS ENGINE actually ships. Ollama
        # and llama.cpp tags are quantised by default — `llama3.1:8b` is a 4.9 GB
        # Q4_K_M, and reading it at half precision called it 16.8 GB and would
        # have warned about a model that fits a laptop with room to spare. A
        # HuggingFace repo really does ship fp16/bf16 unless its name says
        # otherwise, which is the case the AWQ/GPTQ branch above catches.
        per = 0.60 if (engine or "").strip().lower() in _QUANTISED_ENGINES else 2.10
    return round(billions * per, 1)
