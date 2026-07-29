"""Reader for deploy/platforms.conf — the platform support matrix SSOT.

`hwinfo` answers "what hardware is this?". This module answers "what do we KNOW
about that hardware, and how strongly?" — the install profile, the engine that can
actually serve there, where watts come from, and the verification tier.

Kept separate from `hwinfo` deliberately: hwinfo reads the machine, this reads a
table. Mixing them would put a docs concern on the fit path.

Stdlib only and no `ava_bridge` imports beyond nothing at all, because
`deploy/install.sh` shells into it on a bare system before anything is installed —
the same reason `perf_log.py` stays import-free.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

_CONF_REL = os.path.join("deploy", "platforms.conf")
_COLUMNS = ("key", "platform_id", "mem_model", "label", "profile", "engine",
            "power_source", "nominal_w", "tier", "evidence")

# Tiers, strongest first. A tier ABOVE ci-simulated must cite evidence; the guard
# in tests/test_platform_matrix_ssot.py enforces that rather than trusting review.
TIERS = ("verified-on-device", "ci-native", "ci-simulated",
         "community-reported", "unsupported")
EVIDENCE_REQUIRED = ("verified-on-device", "ci-native", "community-reported")


@dataclass
class Platform:
    key: str = ""
    platform_id: str = ""
    mem_model: str = ""
    label: str = ""
    profile: str = ""
    engine: str = ""
    power_source: str = ""
    nominal_w: float | None = None
    tier: str = ""
    evidence: str = ""
    warnings: list[str] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        """True only for a tier backed by real hardware of this class."""
        return self.tier in ("verified-on-device", "ci-native")

    @property
    def power_measurable(self) -> bool:
        return self.power_source not in ("", "none", "-")

    def summary(self) -> str:
        """One line for `ava doctor` / install.sh. Names the tier, always."""
        s = f"{self.label} [{self.tier}]"
        if self.tier == "ci-simulated":
            s += " — logic tested, numbers unconfirmed on this hardware"
        elif self.tier == "unsupported":
            s += " — not a supported target"
        return s


def _conf_path() -> str:
    override = os.environ.get("AVA_PLATFORMS_CONF")
    if override:
        return override
    # <repo>/ava_bridge/platforms.py -> <repo>/deploy/platforms.conf
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        _CONF_REL)


def _num(v: str) -> float | None:
    try:
        return float(v)
    except ValueError:
        return None


def load() -> list[Platform]:
    """Every row, in file order. Malformed rows are skipped, not fatal.

    A broken table must not brick `ava doctor` on a box whose only problem is a
    stale checkout — the caller degrades to "unknown platform" instead.
    """
    out: list[Platform] = []
    try:
        with open(_conf_path(), encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return out
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != len(_COLUMNS):
            continue
        d = dict(zip(_COLUMNS, parts))
        out.append(Platform(
            key=d["key"], platform_id=d["platform_id"], mem_model=d["mem_model"],
            label=d["label"], profile=d["profile"], engine=d["engine"],
            power_source=d["power_source"], nominal_w=_num(d["nominal_w"]),
            tier=d["tier"], evidence=d["evidence"]))
    return out


def for_platform(platform_id: str, mem_model: str | None = None) -> Platform | None:
    """The row for a platform class, narrowed by memory model when given.

    `linux-nvidia` matches two rows (unified GB10 vs discrete RTX) because hwinfo
    cannot tell them apart from the platform id alone — `mem_model` is what
    disambiguates, and it comes from `hwinfo.fit_memory()`/`_memory_model()`.
    Without it, the first row wins, which is the documented first-match-wins rule.
    """
    rows = [p for p in load() if p.platform_id == platform_id]
    if not rows:
        return None
    if mem_model:
        for p in rows:
            if p.mem_model == mem_model:
                return p
    return rows[0]


def detect() -> Platform | None:
    """The row describing the machine this is running on.

    Imports hwinfo lazily so `install.sh` can read the table on a bare system.
    """
    try:
        from . import hwinfo
    except Exception:  # noqa: BLE001 — no package context (script use)
        return None
    pid = hwinfo.platform_id()
    fit = hwinfo.fit_memory()
    model, _why = hwinfo._memory_model()
    if model == "unknown":
        # fit_memory already resolved this; infer from what it chose rather than
        # asking again, so the table and the fit path cannot disagree.
        src = fit.source or ""
        if src.startswith("vram"):
            model = "discrete"
        elif src.startswith("system"):
            model = "unified" if pid in ("linux-nvidia", "darwin-apple") else "system"
    return for_platform(pid, model)
