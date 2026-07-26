"""Driver registry — name -> adapter class, with a user drop-in directory.

Selection is a config string (`alloc.models.<id>.driver`), resolved here, in the
same shape `ava_bridge/runtime/__init__.py` resolves an agent runtime: a dict with
aliases, and a resolver that falls back to a graceful floor rather than raising.

**Adding support for a new engine is one file and one config block.** Built-in
adapters live beside this module; a fork's own live in
`$AVA_HOME/alloc_drivers/*.py` and are loaded after the built-ins, overriding by
`name` — the same built-in-then-user precedence connectors use. Nothing in the core
needs to change, and nothing in the core knows any particular engine's name.

The floor is `ObserveDriver`: an unknown driver name, missing tooling, or a model
hosted inside another one resolves to "report it, never touch it". That keeps an
unresolvable declaration *visible* instead of turning it into a silent no-op the
planner would mistake for reclaimable memory.
"""
from __future__ import annotations

import os

from ..base import DriverContext, ModelDriver
from .docker import DockerDriver
from .http_unload import HttpUnloadDriver
from .observe import ObserveDriver
from .systemd import SystemdDriver

_REGISTRY: dict[str, type[ModelDriver]] = {
    "docker": DockerDriver, "container": DockerDriver,
    "systemd": SystemdDriver, "unit": SystemdDriver, "service": SystemdDriver,
    "http-unload": HttpUnloadDriver, "http_unload": HttpUnloadDriver,
    "observe": ObserveDriver, "none": ObserveDriver, "": ObserveDriver,
}

_user_loaded = False


def registry() -> dict[str, type[ModelDriver]]:
    _load_user_drivers()
    return dict(_REGISTRY)


def register(cls: type[ModelDriver]) -> None:
    """Add or replace an adapter by its `name`. Used by user drop-ins and tests."""
    name = getattr(cls, "name", "") or ""
    if name:
        _REGISTRY[name.strip().lower()] = cls


def for_spec(driver_name: str, ctx: DriverContext) -> ModelDriver:
    """The driver for one declared model.

    Resolves the configured class, then checks it can actually run here: an adapter
    whose tooling is missing (no container runtime, wrong platform) reports that
    from `validate()` and is replaced by the observe floor. So a fork on hardware
    the adapter does not support degrades to honest reporting rather than to errors
    at actuation time.
    """
    _load_user_drivers()
    cls = _REGISTRY.get((driver_name or "").strip().lower())
    if cls is None:
        return ObserveDriver(ctx, reason=f"unknown driver {driver_name!r}")
    try:
        drv = cls(ctx)
    except Exception as e:  # noqa: BLE001 — a bad adapter must not break reporting
        return ObserveDriver(ctx, reason=f"driver {driver_name!r} failed to load: {e}")
    if not drv.usable():
        return ObserveDriver(ctx, reason=drv.unusable_reason())
    return drv


def _load_user_drivers() -> None:
    """Import `$AVA_HOME/alloc_drivers/*.py` once; each may call `register()`.

    Failures are per-file and non-fatal: one broken drop-in must not cost an
    operator every other driver, so it is skipped and surfaced by `ava doctor`.
    """
    global _user_loaded
    if _user_loaded:
        return
    _user_loaded = True
    try:
        from ... import settings  # noqa: PLC0415 — lazy: avoids an import cycle
        d = os.path.join(settings.home(), "alloc_drivers")
        if not os.path.isdir(d):
            return
        import importlib.util
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".py") or fn.startswith(("_", ".")):
                continue
            path = os.path.join(d, fn)
            try:
                sp = importlib.util.spec_from_file_location(f"ava_alloc_user_{fn[:-3]}", path)
                if sp and sp.loader:
                    mod = importlib.util.module_from_spec(sp)
                    sp.loader.exec_module(mod)
                    cls = getattr(mod, "DRIVER", None)
                    if cls is not None:
                        register(cls)
            except Exception:  # noqa: BLE001 — one bad drop-in, not all of them
                continue
    except Exception:  # noqa: BLE001 — no settings/home; built-ins are enough
        return
