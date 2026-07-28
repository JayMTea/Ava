"""ModelSpec + load_models() — the one place a declared model is defined.

Ava already describes models in two places, and neither can drive an allocator:

  * `models:` is a **download manifest** — role -> engine/id/tier/url. It says what
    to fetch, and nothing about memory.
  * `inference.backends.<id>.fit` is a **serving profile** — weight_gb, min_free_gb,
    tier, workloads. It is keyed by language-model backend, so an image checkpoint
    or a voice model has no place in it.

Neither carries a driver, a priority, or a release cost. So allocation gets a third
block, `alloc:` — kept deliberately thin by **inheriting instead of duplicating**:

  1. every `inference.backends.<id>` that is local is auto-declared, taking its
     `weight_gb` / `min_free_gb` / `tier` / `workloads` straight from the existing
     `fit:` block via `model_fit.profile_for()`;
  2. every connector with a `service:` block contributes its unit and probe.

So `alloc.models.<id>` only has to say what neither of the others can: which driver
actuates it, how it ranks against the others, and what releasing it costs. A number
declared under `fit:` is never restated here.

This module is the normalizer, in the same role `router_app.load_backends()` plays
for backends: one function, one output shape, every consumer downstream of it. It
also stamps provenance (`source`, `implicit`) so a UI can say "inherited from your
backend config" rather than presenting it as a hand-authored choice.

**Absent `alloc:` block means an empty registry**, which means every lease is
granted and nothing is ever released — exactly the behaviour that shipped before
this layer existed. A partially-declared model is not an error either: a missing
`weight_gb` is UNKNOWN, and unknown never gates.

Reads, with defaults that make an unconfigured install behave as it always did:

    alloc:
      enabled: true                 # [AVA_ALLOC]
      pool:
        baseline: measure           # measure | <GiB>
        min_free_gib: 4             # [AVA_ALLOC_MIN_FREE]
      defaults:
        priority: batch
      models:
        <model-id>:
          driver: docker            # registry key; unknown -> observe-only
          priority: interactive     # interactive | batch | background
          pinned: false             # never released, at any priority
          via: null                 # hosted INSIDE another declared model
          host: null                # null = this box; anything else is observed
          weight_gb: 24             # inherited from inference.backends.<id>.fit
          readiness: {...}          # how to tell resident from merely listening
          release:  {unload_s: 8, stop_s: 30}
          restore:  {warm_s: 30, cold_s: 420, debounce_s: 90, auto: true}
          driver_config: {...}      # opaque to core; the driver alone reads it

**Scope.** This resolves declarations. It does not measure the pool (`capacity`),
decide anything (`policy`), or actuate (`drivers`).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .. import settings

# Priority ranks. Lower wins. `pinned` is not a priority — it is a separate flag
# that removes a model from candidacy entirely, so an operator can protect a model
# without having to reason about where it sits in the ordering.
PRIORITIES = ("interactive", "batch", "background")
_RANK = {name: i for i, name in enumerate(PRIORITIES)}
DEFAULT_PRIORITY = "batch"

# Kept identical to model_fit's default so a model declared in neither place still
# leaves working headroom behind it.
DEFAULT_MIN_FREE_GB = 4.0


@dataclass
class ModelSpec:
    """One declared model: what it costs, how it ranks, and how to actuate it."""

    id: str
    label: str = ""
    driver: str = ""                        # registry key; "" -> inferred, then observe
    priority: str = DEFAULT_PRIORITY
    pinned: bool = False
    via: str | None = None                  # hosted inside another declared model
    host: str | None = None                 # None = this box
    weight_gb: float | None = None          # None = UNKNOWN, never 0
    min_free_gb: float = DEFAULT_MIN_FREE_GB
    tier: str = ""
    workloads: frozenset = field(default_factory=frozenset)
    readiness: dict = field(default_factory=dict)
    release: dict = field(default_factory=dict)
    restore: dict = field(default_factory=dict)
    driver_config: dict = field(default_factory=dict)
    # provenance, for reports and UIs
    source: str = "alloc"                   # alloc | backends | connector
    implicit: bool = False                  # derived rather than hand-authored
    notes: list[str] = field(default_factory=list)

    @property
    def rank(self) -> int:
        return _RANK.get(self.priority, _RANK[DEFAULT_PRIORITY])

    @property
    def local(self) -> bool:
        """False when declared on another host — reported, never actuated."""
        return not self.host

    @property
    def need_gib(self) -> float | None:
        """Free memory required to bring this model up: its weight plus the
        headroom it wants left behind. None when the weight is unknown, which the
        planner reads as "cannot compute — do not gate"."""
        if self.weight_gb is None:
            return None
        return self.weight_gb + self.min_free_gb


def enabled() -> bool:
    """Is the allocation layer switched on? Default True, because with no declared
    models it is inert anyway — an operator who declares nothing sees no change."""
    return settings.get_bool("alloc.enabled", True, env="AVA_ALLOC")


def role_model(role: str) -> str | None:
    """The declared model id that serves a logical role, or None.

    Ava's own subsystems know they are about to use "the image engine", not which
    model id an operator called it. This maps one to the other:

        alloc:
          roles:
            image: my-image-engine

    Unset means **take no lease** — the subsystem behaves exactly as it did before
    this layer. That is what keeps the funnel opt-in: declaring a model does not
    silently start gating a code path until the operator also says which role it
    fills. Returns None for a role pointing at a model that is not declared, so a
    stale mapping degrades to "uncoordinated" rather than to an error.
    """
    mid = settings.get(f"alloc.roles.{role}")
    if not mid:
        return None
    mid = str(mid).strip()
    return mid if mid in {s.id for s in load_models()} else None


def pool_config() -> dict:
    """Pool-level knobs. `baseline: measure` means LEARN this box's non-AI floor
    rather than carry a number from someone else's machine."""
    raw = settings.get("alloc.pool") or {}
    baseline = raw.get("baseline", "measure")
    if isinstance(baseline, str) and baseline.strip().lower() != "measure":
        baseline = _as_float(baseline)
    return {
        "baseline": baseline,
        "min_free_gib": _as_float(
            settings.get("alloc.pool.min_free_gib", env="AVA_ALLOC_MIN_FREE")) or 4.0,
        "speculative_max_s": _as_float(raw.get("speculative_max_s")) or 15.0,
    }


def load_models() -> list[ModelSpec]:
    """Every declared model, from `alloc.models` plus the two auto-ingest sources.

    Order is stable and meaningful for reports: explicitly declared first, then
    inherited. Never raises — a broken block yields fewer models, never an
    exception, because an allocator that cannot read its config must degrade to
    "govern nothing", not take the bridge down with it.
    """
    try:
        declared = settings.get("alloc.models") or {}
        if not isinstance(declared, dict):
            declared = {}
    except Exception:  # noqa: BLE001 — a broken config must not break the caller
        declared = {}

    specs: dict[str, ModelSpec] = {}

    # 1. explicit declarations
    for mid, raw in declared.items():
        if not isinstance(raw, dict):
            continue
        specs[str(mid)] = _spec_from_alloc(str(mid), raw)

    # 2. inherit sizing from serving profiles the operator already wrote
    for bid, prof in _backend_profiles().items():
        spec = specs.get(bid)
        if spec is None:
            spec = ModelSpec(id=bid, source="backends", implicit=True,
                             priority=_default_priority())
            specs[bid] = spec
        _inherit_fit(spec, prof)

    # 3. inherit unit/probe from connector manifests
    for svc in _connector_services():
        spec = specs.get(svc["id"])
        if spec is None:
            continue  # a connector alone does not declare a model as governable
        _inherit_service(spec, svc)

    for spec in specs.values():
        _finalize(spec)
    return list(specs.values())


def by_id() -> dict[str, ModelSpec]:
    return {s.id: s for s in load_models()}


# --- construction ----------------------------------------------------------- #
def _spec_from_alloc(mid: str, raw: dict) -> ModelSpec:
    prio = str(raw.get("priority") or _default_priority()).strip().lower()
    if prio not in _RANK:
        prio = _default_priority()
    return ModelSpec(
        id=mid,
        label=str(raw.get("label") or mid),
        driver=str(raw.get("driver") or "").strip().lower(),
        priority=prio,
        pinned=bool(raw.get("pinned", False)),
        via=(str(raw["via"]).strip() or None) if raw.get("via") else None,
        host=(str(raw["host"]).strip() or None) if raw.get("host") else None,
        weight_gb=_as_float(raw.get("weight_gb")),
        min_free_gb=_as_float(raw.get("min_free_gb")) or DEFAULT_MIN_FREE_GB,
        tier=str(raw.get("tier") or "").strip().lower(),
        readiness=_expand_all(raw.get("readiness") or {}),
        release=dict(raw.get("release") or {}),
        restore=dict(raw.get("restore") or {}),
        driver_config=_expand_all(raw.get("driver_config") or {}),
        source="alloc",
    )


def _inherit_fit(spec: ModelSpec, prof) -> None:
    """Fill sizing from a `model_fit.FitProfile` without overriding a declaration.

    The operator's explicit `alloc.models.<id>` value always wins; this only fills
    blanks, so a number lives in exactly one place in their config.
    """
    if spec.weight_gb is None and getattr(prof, "weight_gb", None) is not None:
        spec.weight_gb = prof.weight_gb
        spec.notes.append("weight_gb inherited from inference.backends fit")
    if spec.min_free_gb == DEFAULT_MIN_FREE_GB:
        mf = getattr(prof, "min_free_gb", None)
        if mf:
            spec.min_free_gb = float(mf)
    if not spec.tier:
        spec.tier = getattr(prof, "tier", "") or ""
    if not spec.workloads:
        spec.workloads = getattr(prof, "workloads", frozenset()) or frozenset()
    # A backend the fit layer calls remote does not consume this box's memory.
    if spec.host is None and getattr(prof, "local", True) is False:
        spec.host = "remote"
        spec.notes.append("remote backend — observed, never actuated")


def _inherit_service(spec: ModelSpec, svc: dict) -> None:
    """Fill unit/probe defaults from a connector manifest."""
    if svc.get("unit") and not spec.driver_config.get("unit"):
        spec.driver_config["unit"] = svc["unit"]
        spec.notes.append(f"unit inherited from connector {svc['id']}")
    if svc.get("probe") and not spec.readiness.get("url"):
        spec.readiness["url"] = svc["probe"]
        spec.notes.append(f"probe inherited from connector {svc['id']}")


def _finalize(spec: ModelSpec) -> None:
    """Infer a driver when none was declared, and record why.

    Inference is a convenience, never a surprise: it only ever fires when the
    operator left `driver:` blank, and whatever it picks is stated in `notes` so a
    report can show the reasoning. An unresolved model becomes observe-only, which
    keeps it *visible* instead of silently ungoverned.
    """
    if not spec.label:
        spec.label = spec.id
    if not spec.local:
        spec.driver = spec.driver or "observe"
        return
    if spec.via:
        # Hosted inside another model: its host is what gets actuated.
        spec.driver = spec.driver or "observe"
        spec.notes.append(f"hosted via {spec.via} — released with its host")
        return
    if spec.driver:
        return
    cfg = spec.driver_config
    if cfg.get("container"):
        spec.driver = "docker"
        spec.notes.append("driver inferred from driver_config.container")
    elif cfg.get("unit"):
        spec.driver = "systemd"
        spec.notes.append("driver inferred from driver_config.unit")
    elif cfg.get("path") or cfg.get("base"):
        spec.driver = "http-unload"
        spec.notes.append("driver inferred from driver_config.base/path")
    else:
        spec.driver = "observe"
        spec.notes.append("no driver declared or inferable — observed only")


# --- ingest sources --------------------------------------------------------- #
def _backend_profiles() -> dict:
    """`{backend_id: FitProfile}` for local backends, or {} if unavailable.

    Imported lazily and defensively: the allocator is useful without the router,
    and a fork may have neither backends nor a `fit:` block anywhere.
    """
    try:
        from .. import model_fit
        from ..router_app import load_backends
    except Exception:  # noqa: BLE001 — no router/fit available; nothing to inherit
        return {}
    out = {}
    try:
        for b in load_backends() or []:
            bid = b.get("id")
            if not bid:
                continue
            out[str(bid)] = model_fit.profile_for(b)
    except Exception:  # noqa: BLE001 — never let config break the allocator
        return {}
    return out


def _connector_services() -> list[dict]:
    try:
        from .. import connectors
        return connectors.services() or []
    except Exception:  # noqa: BLE001 — connectors are optional
        return []


def _expand_all(obj):
    """Expand `${VAR}` / `${VAR:-default}` through a nested config value.

    Reuses the connector manifest expander so one syntax works everywhere in Ava:
    a URL or unit name written once serves bare metal and a container, where the
    host and port differ. Without this a declaration like
    `base: "${AVA_GPU_SERVICE:-http://127.0.0.1:8189}"` would reach the driver verbatim
    and build a nonsense address.

    Falls back to the value unchanged when connectors are unavailable — a trimmed
    fork without them still gets literal values, which is the pre-expansion
    behaviour rather than a failure.
    """
    try:
        from ..connectors import _expand
    except Exception:  # noqa: BLE001 — connectors optional; leave values as written
        return obj if not isinstance(obj, dict) else dict(obj)
    if isinstance(obj, dict):
        return {k: _expand_all(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_expand_all(v) for v in obj]
    return _expand(obj)


def _default_priority() -> str:
    p = str(settings.get("alloc.defaults.priority", DEFAULT_PRIORITY) or "").strip().lower()
    return p if p in _RANK else DEFAULT_PRIORITY


def _as_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None
